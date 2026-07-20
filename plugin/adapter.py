"""fmsg platform adapter (Hermes plugin).

Connects Hermes Agent to an fmsg host through fmsg-webapi: real-time
inbound over the ``/fmsg/ws`` WebSocket, outbound via the JSON REST
routes. Auth is an fmsg API key (``fmsgk_...``) exchanged for a
short-lived JWT — run the agent as a derived sub-account such as
``@alice_hermes@example.com``.

Configuration in config.yaml::

    platforms:
      fmsg:
        enabled: true
        extra:
          api_url: "https://fmsgapi.example.com"
          api_key: "fmsgk_..."
          default_topic: "Hermes"

Environment variables (env wins over config.yaml ``extra``; names match
fmsg-cli so one .env can drive both):

    FMSG_API_URL            Base URL of fmsg-webapi
                            (default "https://api.fmsg.io")
    FMSG_API_KEY            API key fmsgk_<key_id>_<secret> (required)
    FMSG_ALLOWED_USERS      Comma-separated @user@domain allowlist
    FMSG_ALLOW_ALL_USERS    Allow any sender — dev only
    FMSG_HOME_CHANNEL       @user@domain for cron / notification delivery
    FMSG_HOME_CHANNEL_NAME  Human label for the home channel
    FMSG_DEFAULT_TOPIC      Topic for agent-initiated root messages
                            (default "Hermes")

Identity/threading model: the counterparty fmsg address is both
``chat_id`` and ``user_id`` (fmsg addresses are authenticated by the
host, so the allowlist is a real trust boundary). An fmsg conversation
is a **tree** (root + ``pid`` replies that may branch). Hermes sessions
are linear, so this adapter maps each **branch** of that tree to a
session:

* First reply to a parent continues the parent's Hermes session
  (``thread_id`` stays the branch key, usually the root id).
* A later reply to a parent that already has a child is a **fork**:
  new ``thread_id`` = ``{root_id}:br:{msg_id}``, and the direct
  ancestry (root → … → parent) is injected as ``channel_context`` so
  the new session is not blank — similar in spirit to Hermes
  ``/branch``, but ancestry-only rather than copying sibling history.

Hermes core ``/branch`` copies the *full* transcript onto the same
routing key; adapters cannot call that API, so fmsg implements the
fork by changing ``thread_id`` (new session key) + hydrating ancestry.

Reply-all default: when replying (``pid`` set), outbound ``to`` is every
participant on **the parent message** (``from`` + ``to`` + ``add_to``),
excluding the agent. Keep the full parent recipient set unless there is
an exceptional reason to subset (e.g. privately warning others about a
malicious participant) via metadata ``fmsg_to`` / ``recipients`` or
``fmsg_reply_all=False``. Agent-initiated roots (no ``pid``) stay
single-recipient.

Multi-message agent turns chain: the first outbound replies to the
inbound parent; each further outbound in the same thread replies to the
previous outbound (not all siblings of the user prompt). A new inbound
resets the chain so the next reply targets the new user message.

Agent-initiated sends (no ``reply_to`` / no ``thread_id`` — gateway
home-channel online/offline notices, cron without a thread, etc.) continue
the latest **1:1 DM** with that chat address instead of opening a new root
every time. Force a fresh root with metadata ``fmsg_new_thread=true``
(optional ``topic`` override; default ``FMSG_DEFAULT_TOPIC``).

If reply fails e.g. parent not found, the adapter falls back to a new root
message (with the same topic as the parent if known). This is a 
best-effort fallback to avoid losing the agent's output.
"""

import asyncio
import json
import logging
import os
import random
import time
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import httpx  # noqa: F401  (fmsg_client needs it)
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

try:
    import websockets  # noqa: F401
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_media_bytes,
    get_inbound_media_max_bytes,
)

try:
    from .fmsg_client import (
        FmsgApiError,
        FmsgAuthError,
        FmsgClient,
        TokenManager,
TOKEN_REFRESH_BEFORE,
    )
except ImportError:  # loaded outside a package context
    import importlib.util as _ilu

    _spec = _ilu.spec_from_file_location(
        "hermes_fmsg_client", Path(__file__).parent / "fmsg_client.py"
    )
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    FmsgApiError = _mod.FmsgApiError
    FmsgAuthError = _mod.FmsgAuthError
    FmsgClient = _mod.FmsgClient
    TokenManager = _mod.TokenManager
    TOKEN_REFRESH_BEFORE = _mod.TOKEN_REFRESH_BEFORE

logger = logging.getLogger(__name__)

DEFAULT_TOPIC = "Hermes"
DEFAULT_API_URL = "https://api.fmsg.io"
# Well under the webapi's 10 MB body cap; keeps single messages readable.
MAX_MESSAGE_LENGTH = 65536
MAX_ATTACH_BYTES = 10 * 1024 * 1024  # FMSG_API_MAX_ATTACH_SIZE default
DEDUP_MAX_SIZE = 1000
RECONNECT_BACKOFF_MAX = 60.0
CLOSE_BEFORE_EXPIRY_MIN_SECONDS = 30.0
MIN_WS_CONNECTION_SECONDS = 30.0
CATCHUP_PAGE_LIMIT = 100
FIRST_RUN_CATCHUP_MAX = 50  # unread backlog cap on a brand-new install
# Cap ancestry hydration injected into channel_context on a branch fork.
MAX_ANCESTRY_CONTEXT_MSGS = 20
MAX_ANCESTRY_CONTEXT_CHARS = 8000

_KIND_TO_MESSAGE_TYPE = {
    "image": MessageType.PHOTO,
    "video": MessageType.VIDEO,
    "audio": MessageType.AUDIO,
    "document": MessageType.DOCUMENT,
}


def _extra_or_env(extra: Dict[str, Any], key: str, env: str, default: str = "") -> str:
    return str(extra.get(key) or os.getenv(env, default) or default).strip()


def _norm_addr(addr: str) -> str:
    """Case-fold an fmsg address for equality (Unicode default case folding)."""
    return (addr or "").strip().casefold()


def websocket_rotation_delay(
    expires_at: datetime,
    *,
    now: Optional[datetime] = None,
) -> float:
    """Seconds to keep a WebSocket before reconnecting with a fresh JWT.

    Use the normal five-minute refresh window for long-lived tokens, but scale
    the margin down for short-lived tokens. Subtracting a fixed ten minutes
    caused ten-minute deployments to reconnect every minute without gaining a
    fresher token.
    """
    current = now or datetime.now(timezone.utc)
    remaining = max((expires_at - current).total_seconds(), 0.0)
    refresh_margin = min(
        TOKEN_REFRESH_BEFORE.total_seconds(),
        max(CLOSE_BEFORE_EXPIRY_MIN_SECONDS, remaining * 0.1),
    )
    return max(remaining - refresh_margin, MIN_WS_CONNECTION_SECONDS)


def _merge_addrs(*groups: Any) -> List[str]:
    """Dedupe addresses case-insensitively, preserving first-seen casing/order."""
    out: List[str] = []
    seen: set = set()
    for group in groups:
        if group is None:
            continue
        if isinstance(group, str):
            items = [group]
        else:
            try:
                items = list(group)
            except TypeError:
                continue
        for raw in items:
            if not isinstance(raw, str):
                continue
            addr = raw.strip()
            key = _norm_addr(addr)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(addr)
    return out


def participants_from_msg(msg: Dict[str, Any]) -> List[str]:
    """All participants on a webapi message: from + to + add_to batches."""
    addrs: List[str] = []
    frm = msg.get("from")
    if isinstance(frm, str):
        addrs.append(frm)
    to = msg.get("to") or []
    if isinstance(to, list):
        addrs.extend(a for a in to if isinstance(a, str))
    for batch in msg.get("add_to") or []:
        if isinstance(batch, dict):
            atf = batch.get("add_to_from")
            if isinstance(atf, str):
                addrs.append(atf)
            batch_to = batch.get("to") or []
            if isinstance(batch_to, list):
                addrs.extend(a for a in batch_to if isinstance(a, str))
        elif isinstance(batch, str):
            addrs.append(batch)
    return _merge_addrs(addrs)


def filter_recipients(
    addrs: Any,
    *,
    own_address: str = "",
    fallback: Optional[str] = None,
) -> List[str]:
    """Drop self/empties; fall back to a single counterparty if nothing left."""
    own = _norm_addr(own_address)
    out: List[str] = []
    seen: set = set()
    for raw in addrs or []:
        if not isinstance(raw, str):
            continue
        addr = raw.strip()
        key = _norm_addr(addr)
        if not key or key == own or key in seen:
            continue
        seen.add(key)
        out.append(addr)
    if not out and fallback and _norm_addr(fallback) and _norm_addr(fallback) != own:
        return [fallback.strip()]
    return out


def is_dm_with(msg: Dict[str, Any], chat_id: str, own_address: str = "") -> bool:
    """True when ``msg`` is a 1:1 exchange between ``own_address`` and ``chat_id``.

    Multi-party parents are excluded so agent-initiated home-channel pings
    never attach to a group thread.
    """
    others = filter_recipients(participants_from_msg(msg), own_address=own_address)
    return len(others) == 1 and _norm_addr(others[0]) == _norm_addr(chat_id)


def _truthy_meta(value: Any) -> bool:
    """True when metadata explicitly enables a flag."""
    if value is True:
        return True
    if isinstance(value, (int, float)) and value == 1:
        return True
    if isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on"):
        return True
    return False


def _truthy_meta_false(value: Any) -> bool:
    """True when metadata explicitly disables reply-all."""
    if value is False:
        return True
    if isinstance(value, (int, float)) and value == 0:
        return True
    if isinstance(value, str) and value.strip().lower() in ("0", "false", "no", "off"):
        return True
    return False


def check_requirements() -> bool:
    """Installable and minimally configured (deps + required env)."""
    if not (HTTPX_AVAILABLE and WEBSOCKETS_AVAILABLE):
        return False
    return bool(os.getenv("FMSG_API_KEY", "").strip())


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    api_url = (
        extra.get("api_url") or os.getenv("FMSG_API_URL", DEFAULT_API_URL) or DEFAULT_API_URL
    )
    api_key = extra.get("api_key") or os.getenv("FMSG_API_KEY", "")
    return bool(api_url and api_key)


def is_connected(config) -> bool:
    return validate_config(config)


class FmsgAdapter(BasePlatformAdapter):
    """fmsg adapter: WebSocket inbound, REST outbound via fmsg-webapi."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    supports_code_blocks = True  # bodies are plain text; fences pass through

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform("fmsg"))

        extra = config.extra or {}
        self._api_url = _extra_or_env(
            extra, "api_url", "FMSG_API_URL", DEFAULT_API_URL
        ).rstrip("/")
        self._api_key = _extra_or_env(extra, "api_key", "FMSG_API_KEY")
        self._default_topic = (
            _extra_or_env(extra, "default_topic", "FMSG_DEFAULT_TOPIC") or DEFAULT_TOPIC
        )

        self._tokens: Optional[TokenManager] = None
        self._client: Optional[FmsgClient] = None
        self._ws_task: Optional[asyncio.Task] = None

        # Dedup: message id -> monotonic-ish timestamp, insertion-ordered.
        self._seen_ids: Dict[int, float] = {}
        # message id -> (root id, root topic) for pid-chain resolution.
        self._thread_roots: Dict[int, Tuple[int, Optional[str]]] = {}
        # (chat_id, thread_id) -> last inbound message id, for reply threading.
        self._last_inbound: Dict[Tuple[str, str], int] = {}
        # (chat_id, thread_id) -> last outbound message id, for chaining agent turns.
        self._last_outbound: Dict[Tuple[str, str], int] = {}
        # message id (str) -> participants of that message (from/to/add_to).
        self._msg_participants: Dict[str, List[str]] = {}
        # normalised chat_id -> last 1:1 DM message id (either direction).
        # Used so agent-initiated home/cron pings continue the existing thread.
        self._last_by_chat: Dict[str, int] = {}
        # Branch sessions: parent_id -> child message ids (order of discovery).
        self._children: Dict[int, List[int]] = {}
        # message id -> Hermes thread_id / branch key for that message.
        self._msg_branch: Dict[int, str] = {}
        self._last_seen_id: int = 0
        self._state_path = Path(
            os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))
        ) / "fmsg_last_seen.json"

    # -- Connection lifecycle -------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not (HTTPX_AVAILABLE and WEBSOCKETS_AVAILABLE):
            logger.warning(
                "[%s] missing deps. Run: pip install httpx websockets", self.name
            )
            return False
        if not (self._api_url and self._api_key):
            logger.warning("[%s] FMSG_API_KEY not configured", self.name)
            return False

        self._tokens = TokenManager(self._api_url, self._api_key)
        self._client = FmsgClient(self._tokens)
        self._load_state()
        self._ws_task = asyncio.create_task(self._run_ws())
        self._mark_connected()
        logger.info("[%s] Connected — streaming from %s/fmsg/ws", self.name, self._api_url)
        return True

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None
        if self._client:
            await self._client.aclose()
            self._client = None
            self._tokens = None
        logger.info("[%s] Disconnected", self.name)

    # -- Inbound: WebSocket loop ------------------------------------------------

    async def _run_ws(self) -> None:
        backoff = 1.0
        while self._running:
            stream_start = time.monotonic()
            try:
                # Fresh token up front; FmsgAuthError here is fatal (bad key).
                await self._tokens.get_token()
                self._own_address()  # cache identity for self-filtering
                await self._catch_up()
                await self._consume_ws()
            except asyncio.CancelledError:
                return
            except FmsgAuthError as e:
                logger.error("[%s] API key rejected: %s", self.name, e)
                self._set_fatal_error(
                    "fmsg_unauthorized",
                    f"fmsg-webapi rejected the API key: {e}. Check FMSG_API_KEY.",
                    retryable=False,
                )
                return
            except Exception as e:
                if not self._running:
                    return
                logger.warning("[%s] Stream error: %s", self.name, e)

            if not self._running:
                return
            # Reset backoff after a healthy long-lived connection.
            if time.monotonic() - stream_start >= 60.0:
                backoff = 1.0
            delay = backoff * (0.5 + random.random())
            logger.info("[%s] Reconnecting in %.1fs...", self.name, delay)
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)

    async def _consume_ws(self) -> None:
        """Consume WS events until the token nears expiry, then return to
        reconnect with a fresh token (the handshake JWT is not renewable
        in-band)."""
        deadline = None
        if self._tokens.expires_at is not None:
            deadline = time.monotonic() + websocket_rotation_delay(
                self._tokens.expires_at
            )

        agen = self._client.iter_events()
        try:
            while self._running:
                timeout = None
                if deadline is not None:
                    timeout = deadline - time.monotonic()
                    if timeout <= 0:
                        logger.info("[%s] Token nearing expiry — rotating connection", self.name)
                        return
                try:
                    event = await asyncio.wait_for(agen.__anext__(), timeout=timeout)
                except asyncio.TimeoutError:
                    logger.info("[%s] Token nearing expiry — rotating connection", self.name)
                    return
                except StopAsyncIteration:
                    return
                if event.get("type") == "new_msg" and isinstance(event.get("data"), dict):
                    await self._on_message(event["data"])
                # Unknown event types are ignored (forward compatible).
        finally:
            await agen.aclose()

    async def _catch_up(self) -> None:
        """Dispatch messages that arrived while disconnected.

        Pages the inbox (id-descending) until we pass ``_last_seen_id``. On a
        brand-new install (no persisted state) only unread messages are
        dispatched, capped so an old mailbox doesn't flood the agent.
        """
        first_run = self._last_seen_id == 0
        pending: List[Dict[str, Any]] = []
        offset = 0
        while True:
            page = await self._client.list_messages(limit=CATCHUP_PAGE_LIMIT, offset=offset)
            if not page:
                break
            for msg in page:
                msg_id = msg.get("id") or 0
                if not first_run and msg_id <= self._last_seen_id:
                    page = None
                    break
                if first_run and (msg.get("read") or len(pending) >= FIRST_RUN_CATCHUP_MAX):
                    page = None
                    break
                pending.append(msg)
            if page is None or len(page) < CATCHUP_PAGE_LIMIT:
                break
            offset += CATCHUP_PAGE_LIMIT
        if first_run and self._last_seen_id == 0 and not pending:
            # Nothing to deliver but anchor the high-water mark so future
            # restarts don't rescan history.
            head = await self._client.list_messages(limit=1)
            if head:
                self._record_seen(head[0].get("id") or 0)
        for msg in reversed(pending):  # oldest first
            await self._on_message(msg)

    # -- Inbound: message processing ---------------------------------------------

    async def _on_message(self, msg: Dict[str, Any]) -> None:
        msg_id = msg.get("id")
        if msg_id is None or self._is_duplicate(msg_id):
            return
        sender = msg.get("from") or ""
        if sender == self._own_address():
            self._record_seen(msg_id)
            return

        try:
            text = await self._message_text(msg_id, msg)
            media_urls, media_types, msg_type = await self._cache_attachments(msg_id, msg)
        except FmsgApiError as e:
            logger.warning("[%s] Failed to fetch message %s content: %s", self.name, msg_id, e)
            return

        root_id, topic = await self._resolve_thread_root(msg_id, msg)
        pid = msg.get("pid") if msg.get("has_pid") or msg.get("pid") else None
        # Ensure parent has a branch key before assigning this message.
        if pid and pid not in self._msg_branch:
            self._msg_branch[pid] = str(root_id)
        branch_key, is_fork = self._assign_branch_key(msg_id, pid, root_id)
        self._save_state()

        participants = self._remember_participants(msg_id, msg)

        source = self.build_source(
            chat_id=sender,
            chat_name=sender.lstrip("@").split("@")[0],
            chat_type="dm",
            user_id=sender,
            user_name=sender.lstrip("@").split("@")[0],
            thread_id=branch_key,
            chat_topic=topic,
            message_id=str(msg_id),
        )

        metadata: Dict[str, Any] = {
            "fmsg_root_id": root_id,
            "fmsg_branch_key": branch_key,
            "fmsg_is_fork": is_fork,
        }
        context_notes: List[str] = []
        if is_fork:
            ancestry = await self._ancestry_chain(msg_id, msg)
            ancestry_ctx = await self._format_ancestry_context(ancestry)
            if ancestry_ctx:
                context_notes.append(ancestry_ctx)
        if msg.get("important"):
            metadata["important"] = True
            context_notes.append("[the sender marked this fmsg message as important]")
        if msg.get("no_reply"):
            metadata["no_reply"] = True
            context_notes.append(
                "[the sender flagged no_reply: replies to this fmsg thread will be discarded]"
            )

        others = filter_recipients(
            participants, own_address=self._own_address(), fallback=None
        )
        others_only = [a for a in others if _norm_addr(a) != _norm_addr(sender)]
        if others_only:
            listed = ", ".join(others_only)
            context_notes.append(
                f"[fmsg multi-party message — other participants on this "
                f"message: {listed}. Replies default to all participants of "
                "the parent you reply to; only omit someone in exceptional "
                "cases (e.g. warning others about abuse).]"
            )
            metadata["fmsg_participants"] = others
            metadata["fmsg_multi_party"] = True
        else:
            metadata["fmsg_participants"] = others or ([sender] if sender else [])

        ts = msg.get("time")
        try:
            timestamp = (
                datetime.fromtimestamp(float(ts), tz=timezone.utc)
                if ts
                else datetime.now(tz=timezone.utc)
            )
        except (ValueError, OSError, TypeError):
            timestamp = datetime.now(tz=timezone.utc)

        event = MessageEvent(
            text=text,
            message_type=msg_type,
            source=source,
            message_id=str(msg_id),
            raw_message=msg,
            media_urls=media_urls,
            media_types=media_types,
            reply_to_message_id=str(pid) if pid else None,
            channel_context="\n".join(context_notes) or None,
            metadata=metadata,
            timestamp=timestamp,
        )

        thread_key = (sender, branch_key)
        self._last_inbound[thread_key] = msg_id
        # New user message starts a new agent turn — do not keep chaining
        # to the previous outbound.
        self._last_outbound.pop(thread_key, None)
        # Track pure DMs so agent-initiated sends (home-channel notices) can
        # continue the conversation instead of spawning a new root.
        if not others_only:
            self._note_chat_message(sender, msg_id)
        logger.debug("[%s] Message %s from %s: %s", self.name, msg_id, sender, text[:80])
        await self.handle_message(event)
        self._record_seen(msg_id)
        try:
            await self._client.mark_read(msg_id)
        except FmsgApiError as e:
            logger.debug("[%s] mark_read(%s) failed: %s", self.name, msg_id, e)

    async def _message_text(self, msg_id: int, msg: Dict[str, Any]) -> str:
        """Body text: short_text when it holds the complete body, else /data."""
        mime = (msg.get("type") or "").lower()
        short = msg.get("short_text")
        size = msg.get("size") or 0
        if short is not None and len(short.encode("utf-8")) >= size:
            return short
        if not mime.startswith("text/"):
            return short or ""
        data = await self._client.get_data(msg_id)
        return data.decode("utf-8", errors="replace")

    async def _cache_attachments(
        self, msg_id: int, msg: Dict[str, Any]
    ) -> Tuple[List[str], List[str], MessageType]:
        media_urls: List[str] = []
        media_types: List[str] = []
        msg_type = MessageType.TEXT
        max_bytes = get_inbound_media_max_bytes()
        for att in msg.get("attachments") or []:
            filename = att.get("filename") or "attachment.bin"
            size = att.get("size") or 0
            if max_bytes and size > max_bytes:
                logger.warning(
                    "[%s] Skipping oversized attachment %s (%d bytes) on message %s",
                    self.name, filename, size, msg_id,
                )
                continue
            try:
                data = await self._client.get_attachment(msg_id, filename)
            except FmsgApiError as e:
                logger.warning(
                    "[%s] Failed to download attachment %s of %s: %s",
                    self.name, filename, msg_id, e,
                )
                continue
            mime = mimetypes.guess_type(filename)[0] or ""
            cached = cache_media_bytes(data, filename=filename, mime_type=mime)
            if cached is None:
                continue
            media_urls.append(cached.path)
            media_types.append(cached.media_type)
            if msg_type is MessageType.TEXT:
                msg_type = _KIND_TO_MESSAGE_TYPE.get(cached.kind, MessageType.DOCUMENT)
        return media_urls, media_types, msg_type

    async def _resolve_thread_root(
        self, msg_id: int, msg: Dict[str, Any]
    ) -> Tuple[int, Optional[str]]:
        """Walk the pid chain to the thread root; cache every hop."""
        if msg_id in self._thread_roots:
            return self._thread_roots[msg_id]
        pid = msg.get("pid") if msg.get("has_pid") or msg.get("pid") else None
        if not pid:
            root = (msg_id, msg.get("topic"))
            self._thread_roots[msg_id] = root
            return root
        chain = [msg_id]
        current = pid
        root: Tuple[int, Optional[str]]
        while True:
            if current in self._thread_roots:
                root = self._thread_roots[current]
                break
            try:
                parent = await self._client.get_message(current)
            except FmsgApiError as e:
                # Root unreachable (e.g. pruned) — treat the earliest
                # reachable ancestor as the root so threading still works.
                logger.debug("[%s] pid walk stopped at %s: %s", self.name, current, e)
                root = (current, None)
                break
            ppid = parent.get("pid") if parent.get("has_pid") or parent.get("pid") else None
            if not ppid:
                root = (current, parent.get("topic"))
                break
            chain.append(current)
            current = ppid
        for mid in chain:
            self._thread_roots[mid] = root
        self._thread_roots[current] = root
        return root

    async def _ancestry_chain(
        self, msg_id: int, msg: Dict[str, Any]
    ) -> List[Tuple[int, Dict[str, Any]]]:
        """Return [(id, msg), ...] from root → current message inclusive."""
        chain_rev: List[Tuple[int, Dict[str, Any]]] = [(msg_id, msg)]
        current = msg
        seen = {msg_id}
        while True:
            pid = current.get("pid") if current.get("has_pid") or current.get("pid") else None
            if not pid or pid in seen:
                break
            seen.add(pid)
            try:
                parent = await self._client.get_message(pid) if self._client else None
            except FmsgApiError as e:
                logger.debug("[%s] ancestry walk stopped at %s: %s", self.name, pid, e)
                break
            if not parent:
                break
            chain_rev.append((pid, parent))
            current = parent
        chain_rev.reverse()
        return chain_rev

    def _assign_branch_key(
        self, msg_id: int, pid: Optional[int], root_id: int
    ) -> Tuple[str, bool]:
        """Map message to a Hermes session thread_id; detect forks.

        First child of a parent continues the parent's branch (usually the
        root id). A later sibling forks: new key ``{root}:br:{msg_id}``.

        Returns ``(branch_key, is_fork)``.
        """
        if msg_id in self._msg_branch:
            parent_key = self._msg_branch.get(pid) if pid else None
            key = self._msg_branch[msg_id]
            return key, bool(pid and parent_key and key != parent_key)

        if not pid:
            key = str(root_id)
            self._msg_branch[msg_id] = key
            return key, False

        parent_key = self._msg_branch.get(pid) or str(root_id)
        kids = self._children.setdefault(pid, [])
        prior = [c for c in kids if c != msg_id]
        if not prior:
            # First discovered child continues the parent session.
            if msg_id not in kids:
                kids.append(msg_id)
            self._msg_branch[msg_id] = parent_key
            self._msg_branch.setdefault(pid, parent_key)
            return parent_key, False

        # Fork: parent already has another child.
        if msg_id not in kids:
            kids.append(msg_id)
        key = f"{root_id}:br:{msg_id}"
        self._msg_branch[msg_id] = key
        self._msg_branch.setdefault(pid, parent_key)
        return key, True

    async def _format_ancestry_context(
        self, ancestry: List[Tuple[int, Dict[str, Any]]]
    ) -> str:
        """Build channel_context from root → parent (exclude current leaf)."""
        if len(ancestry) <= 1:
            return ""
        lines = [
            "[fmsg branch context — direct ancestry only (root → parent); "
            "sibling branches are not included. This is a forked path of the "
            "fmsg tree, hydrated like a session branch rather than a blank chat.]"
        ]
        used = 0
        # Exclude the current message (last entry); show ancestors only.
        ancestors = ancestry[:-1][-MAX_ANCESTRY_CONTEXT_MSGS:]
        for mid, amsg in ancestors:
            try:
                body = await self._message_text(mid, amsg)
            except FmsgApiError:
                body = (amsg.get("short_text") or "").strip()
            body = (body or "").strip()
            if len(body) > 500:
                body = body[:500] + "…"
            frm = amsg.get("from") or "?"
            topic = amsg.get("topic")
            head = f"[id={mid} from={frm}"
            if topic:
                head += f" topic={topic}"
            head += "]"
            chunk = f"{head} {body}" if body else head
            if used + len(chunk) + 1 > MAX_ANCESTRY_CONTEXT_CHARS:
                lines.append("[…ancestry truncated…]")
                break
            lines.append(chunk)
            used += len(chunk) + 1
        return "\n".join(lines)

    def _remember_participants(self, msg_id: int, msg: Dict[str, Any]) -> List[str]:
        """Cache participants of this message (keyed by message id)."""
        parts = participants_from_msg(msg)
        self._msg_participants[str(msg_id)] = parts
        return parts

    async def _participants_for_message(self, msg_id: int) -> List[str]:
        """Participants on a specific message — cache hit or GET /fmsg/:id."""
        key = str(msg_id)
        cached = self._msg_participants.get(key)
        if cached is not None:
            return list(cached)
        if not self._client:
            return []
        try:
            msg = await self._client.get_message(msg_id)
        except FmsgApiError as e:
            logger.debug(
                "[%s] participant fetch failed for %s: %s", self.name, msg_id, e
            )
            return []
        parts = participants_from_msg(msg)
        self._msg_participants[key] = parts
        return parts

    async def _resolve_recipients(
        self,
        chat_id: str,
        pid: Optional[int],
        metadata: Optional[Dict[str, Any]],
    ) -> List[str]:
        """Pick outbound ``to``: parent-message reply-all, override, or single DM.

        Default for a reply is **all participants of the parent message**
        (the one identified by ``pid``), excluding self. Subsetting is for
        exceptional cases only (explicit metadata).
        """
        metadata = metadata or {}
        own = self._own_address()

        override = metadata.get("fmsg_to")
        if override is None:
            override = metadata.get("recipients")
        if override is not None:
            if isinstance(override, str):
                override = [override]
            return filter_recipients(override, own_address=own, fallback=chat_id)

        if _truthy_meta_false(metadata.get("fmsg_reply_all", True)):
            return filter_recipients([chat_id], own_address=own, fallback=chat_id)

        # New root (no parent): single recipient.
        if pid is None:
            return filter_recipients([chat_id], own_address=own, fallback=chat_id)

        parent_parts = await self._participants_for_message(pid)
        return filter_recipients(parent_parts, own_address=own, fallback=chat_id)

    # -- Dedup + persistence ---------------------------------------------------

    def _is_duplicate(self, msg_id: int) -> bool:
        if len(self._seen_ids) > DEDUP_MAX_SIZE:
            for key in list(self._seen_ids)[: DEDUP_MAX_SIZE // 2]:
                self._seen_ids.pop(key, None)
        if msg_id in self._seen_ids:
            return True
        self._seen_ids[msg_id] = time.time()
        return False

    def _record_seen(self, msg_id: int) -> None:
        if msg_id > self._last_seen_id:
            self._last_seen_id = msg_id
            self._save_state()

    def _load_state(self) -> None:
        try:
            state = json.loads(self._state_path.read_text())
            self._last_seen_id = int(state.get("last_seen_id", 0))
            raw = state.get("last_by_chat") or {}
            self._last_by_chat = {}
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if not isinstance(k, str):
                        continue
                    try:
                        self._last_by_chat[_norm_addr(k)] = int(v)
                    except (TypeError, ValueError):
                        continue
            self._msg_branch = {}
            raw_branch = state.get("msg_branch") or {}
            if isinstance(raw_branch, dict):
                for k, v in raw_branch.items():
                    try:
                        self._msg_branch[int(k)] = str(v)
                    except (TypeError, ValueError):
                        continue
            self._children = {}
            raw_children = state.get("children") or {}
            if isinstance(raw_children, dict):
                for k, v in raw_children.items():
                    try:
                        parent = int(k)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(v, list):
                        continue
                    kids: List[int] = []
                    for item in v:
                        try:
                            kids.append(int(item))
                        except (TypeError, ValueError):
                            continue
                    if kids:
                        self._children[parent] = kids
        except (OSError, ValueError, json.JSONDecodeError):
            self._last_seen_id = 0
            self._last_by_chat = {}
            self._msg_branch = {}
            self._children = {}

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            # Cap branch maps so the state file cannot grow without bound.
            max_branch = 500
            msg_branch = self._msg_branch
            children = self._children
            if len(msg_branch) > max_branch:
                keep_ids = set(sorted(msg_branch.keys())[-max_branch:])
                msg_branch = {k: v for k, v in msg_branch.items() if k in keep_ids}
                children = {
                    p: [c for c in kids if c in keep_ids or p in keep_ids]
                    for p, kids in children.items()
                    if p in keep_ids or any(c in keep_ids for c in kids)
                }
            self._state_path.write_text(
                json.dumps(
                    {
                        "last_seen_id": self._last_seen_id,
                        "last_by_chat": self._last_by_chat,
                        "msg_branch": {str(k): v for k, v in msg_branch.items()},
                        "children": {str(k): v for k, v in children.items()},
                    }
                )
            )
        except OSError as e:
            logger.debug("[%s] Could not persist last_seen state: %s", self.name, e)

    def _note_chat_message(self, chat_id: str, message_id: int) -> None:
        """Remember the latest 1:1 message with ``chat_id`` (either direction)."""
        key = _norm_addr(chat_id)
        if not key:
            return
        try:
            mid = int(message_id)
        except (TypeError, ValueError):
            return
        prev = self._last_by_chat.get(key, 0)
        if mid > prev:
            self._last_by_chat[key] = mid
            self._save_state()

    # -- Outbound -----------------------------------------------------------------

    def _own_address(self) -> str:
        return (self._tokens and self._tokens.address) or ""

    def _resolve_reply_target(
        self, chat_id: str, reply_to: Optional[str], metadata: Optional[Dict[str, Any]]
    ) -> Tuple[Optional[int], Optional[str]]:
        """Return (pid, topic) for an outbound message — exactly one is set.

        Within a thread, multi-message agent turns chain: the first outbound
        parents to the inbound (or explicit reply_to); each further outbound
        parents to the previous outbound. Gateway often passes the same
        inbound id as ``reply_to`` for every chunk — last_outbound wins so
        those chunks do not all become siblings of the user prompt.

        Agent-initiated sends (no thread_id / reply_to) continue the latest
        known 1:1 DM with ``chat_id`` when available; otherwise open a root
        with the default topic (or metadata ``topic`` when forcing a new
        thread via ``fmsg_new_thread``).
        """
        metadata = metadata or {}
        thread_id = metadata.get("thread_id")
        key = (chat_id, str(thread_id)) if thread_id is not None else None
        force_new = _truthy_meta(metadata.get("fmsg_new_thread"))

        reply_to_id: Optional[int] = None
        if reply_to:
            try:
                reply_to_id = int(reply_to)
            except (TypeError, ValueError):
                reply_to_id = None

        if key is not None:
            last_out = self._last_outbound.get(key)
            last_in = self._last_inbound.get(key)
            if last_out:
                # Chain when gateway re-uses the inbound id as reply_to for
                # every chunk, or when no distinct reply_to is given.
                if (
                    reply_to_id is None
                    or reply_to_id == last_in
                    or reply_to_id == last_out
                ):
                    return last_out, None
                # Distinct explicit parent (e.g. re-target multi-party root).
                return reply_to_id, None
            if reply_to_id is not None:
                return reply_to_id, None
            if last_in:
                return last_in, None
            try:
                return int(thread_id), None  # reply to the thread root
            except (TypeError, ValueError):
                pass
        elif reply_to_id is not None:
            return reply_to_id, None

        if not force_new:
            last_dm = self._last_by_chat.get(_norm_addr(chat_id))
            if last_dm:
                return last_dm, None

        topic = metadata.get("topic") if force_new else None
        if isinstance(topic, str) and topic.strip():
            return None, topic.strip()
        return None, self._default_topic

    def _record_outbound(
        self,
        chat_id: str,
        message_id: int,
        pid: Optional[int],
        recipients: List[str],
        metadata: Optional[Dict[str, Any]],
    ) -> None:
        """Remember this send so the next agent message in-thread can chain.

        Also caches participants of the outbound message so a chained
        follow-up reply-alls to the same set without an extra GET.
        """
        metadata = metadata or {}
        thread_id = metadata.get("thread_id")
        if thread_id is None and pid is not None:
            if pid in self._thread_roots:
                thread_id = self._thread_roots[pid][0]
            else:
                # Walk known roots; otherwise use pid as a temporary key
                # until an inbound establishes the real root.
                thread_id = pid
        if thread_id is not None:
            self._last_outbound[(chat_id, str(thread_id))] = message_id
        own = self._own_address()
        self._msg_participants[str(message_id)] = _merge_addrs(
            [own] if own else [], recipients
        )
        # Keep branch maps coherent for replies to our own messages.
        if pid is None:
            self._msg_branch[message_id] = str(message_id)
        else:
            if pid not in self._msg_branch:
                # Prefer the Hermes thread we are already in.
                self._msg_branch[pid] = (
                    str(thread_id) if thread_id is not None else str(pid)
                )
            # Root id is used only when minting a fork key "{root}:br:{msg}".
            root_i = pid
            if pid in self._thread_roots:
                root_i = self._thread_roots[pid][0]
            elif isinstance(thread_id, str) and ":br:" in thread_id:
                try:
                    root_i = int(thread_id.split(":br:", 1)[0])
                except ValueError:
                    root_i = pid
            elif str(thread_id or "").isdigit():
                root_i = int(thread_id)
            self._assign_branch_key(message_id, pid, root_i)
        self._save_state()
        # Pure 1:1 outbound → remember for the next agent-initiated ping.
        if len(recipients) == 1 and _norm_addr(recipients[0]) == _norm_addr(chat_id):
            self._note_chat_message(chat_id, message_id)

    async def _lookup_last_dm_message(self, chat_id: str) -> Optional[int]:
        """Find the latest 1:1 message with ``chat_id`` (memory, then API).

        Used after a cold start so gateway home-channel notices still attach
        to the existing DM thread rather than opening a new root.
        """
        key = _norm_addr(chat_id)
        if not key:
            return None
        cached = self._last_by_chat.get(key)
        if cached:
            return cached
        if not self._client:
            return None
        own = self._own_address()
        best: Optional[int] = None
        try:
            inbox = await self._client.list_messages(limit=50)
            sent = await self._client.list_sent(limit=50)
        except (FmsgApiError, FmsgAuthError) as e:
            logger.debug("[%s] last-DM lookup failed for %s: %s", self.name, chat_id, e)
            return None
        for msg in list(inbox or []) + list(sent or []):
            if not isinstance(msg, dict):
                continue
            mid = msg.get("id")
            if mid is None:
                continue
            if not is_dm_with(msg, chat_id, own):
                continue
            try:
                mid_i = int(mid)
            except (TypeError, ValueError):
                continue
            if best is None or mid_i > best:
                best = mid_i
        if best is not None:
            self._note_chat_message(chat_id, best)
        return best

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return await self._send_message(chat_id, content, reply_to=reply_to, metadata=metadata)

    async def _send_message(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        attachments: Optional[List[Tuple[str, bytes]]] = None,
    ) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="fmsg client not initialized")
        from_addr = self._own_address()
        if not from_addr:
            try:
                await self._tokens.get_token()
                from_addr = self._own_address()
            except (FmsgAuthError, FmsgApiError) as e:
                return SendResult(success=False, error=str(e))

        if len(content) > self.MAX_MESSAGE_LENGTH:
            logger.warning(
                "[%s] Message truncated from %d to %d chars",
                self.name, len(content), self.MAX_MESSAGE_LENGTH,
            )
            content = content[: self.MAX_MESSAGE_LENGTH]

        pid, topic = self._resolve_reply_target(chat_id, reply_to, metadata)
        metadata = metadata or {}
        # Cold start / empty cache: look up the latest 1:1 DM so home-channel
        # gateway notices keep the existing thread after a restart.
        if (
            pid is None
            and topic is not None
            and not _truthy_meta(metadata.get("fmsg_new_thread"))
        ):
            found = await self._lookup_last_dm_message(chat_id)
            if found is not None:
                pid, topic = found, None
        recipients = await self._resolve_recipients(chat_id, pid, metadata)
        draft_id: Optional[int] = None
        try:
            draft_id = await self._client.create_draft(
                from_addr, recipients, content, pid=pid, topic=topic
            )
            for filename, data in attachments or []:
                await self._client.attach(draft_id, filename, data)
            await self._client.send(draft_id)
            self._record_outbound(chat_id, draft_id, pid, recipients, metadata)
            return SendResult(success=True, message_id=str(draft_id))
        except FmsgAuthError as e:
            return SendResult(success=False, error=str(e))
        except FmsgApiError as e:
            if draft_id is not None:
                try:
                    await self._client.delete_draft(draft_id)
                except FmsgApiError:
                    pass
            # 400 on a reply can mean the pid was bad — a retry won't help;
            # 5xx and network-level errors are worth retrying.
            return SendResult(success=False, error=str(e), retryable=e.status_code >= 500)
        except Exception as e:
            logger.error("[%s] Send error: %s", self.name, e)
            return SendResult(success=False, error=str(e), retryable=True)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """fmsg has no typing indicator."""

    async def _send_file(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str],
        file_name: Optional[str],
        reply_to: Optional[str],
        metadata: Optional[Dict[str, Any]],
    ) -> SendResult:
        path = Path(file_path)
        try:
            data = path.read_bytes()
        except OSError as e:
            return SendResult(success=False, error=f"cannot read {file_path}: {e}")
        if len(data) > MAX_ATTACH_BYTES:
            return SendResult(
                success=False,
                error=f"attachment {path.name} exceeds fmsg {MAX_ATTACH_BYTES // (1024*1024)} MB limit",
                error_kind="too_long",
            )
        return await self._send_message(
            chat_id,
            caption or "",
            reply_to=reply_to,
            metadata=metadata,
            attachments=[(file_name or path.name, data)],
        )

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        # image_url may be a remote URL (generated images) or a local path.
        if image_url.startswith(("http://", "https://")):
            try:
                async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                    resp = await client.get(image_url)
                    resp.raise_for_status()
                    data = resp.content
            except Exception as e:
                return SendResult(success=False, error=f"failed to fetch image: {e}")
            name = image_url.split("/")[-1].split("?")[0] or "image.jpg"
            if "." not in name:
                name += ".jpg"
            if len(data) > MAX_ATTACH_BYTES:
                return SendResult(success=False, error="image exceeds fmsg attachment limit", error_kind="too_long")
            return await self._send_message(
                chat_id, caption or "", reply_to=reply_to, metadata=metadata,
                attachments=[(name, data)],
            )
        return await self._send_file(chat_id, image_url, caption, None, reply_to, metadata)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_file(chat_id, file_path, caption, file_name, reply_to, metadata)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_file(chat_id, audio_path, caption, None, reply_to, metadata)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_file(chat_id, video_path, caption, None, reply_to, metadata)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {
            "id": chat_id,
            "name": chat_id.lstrip("@").split("@")[0],
            "type": "dm",
            "platform": "fmsg",
        }


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def _env_enablement() -> Optional[dict]:
    """Seed PlatformConfig.extra from env so env-only setups auto-enable."""
    api_url = os.getenv("FMSG_API_URL", DEFAULT_API_URL).strip() or DEFAULT_API_URL
    api_key = os.getenv("FMSG_API_KEY", "").strip()
    if not api_key:
        return None
    seed: dict = {"api_url": api_url.rstrip("/"), "api_key": api_key}
    default_topic = os.getenv("FMSG_DEFAULT_TOPIC", "").strip()
    if default_topic:
        seed["default_topic"] = default_topic
    home = os.getenv("FMSG_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("FMSG_HOME_CHANNEL_NAME", "").strip() or home,
        }
    return seed


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Out-of-process send for cron / send_message_tool when no live adapter."""
    if not HTTPX_AVAILABLE:
        return {"error": "fmsg standalone send: httpx not installed"}
    extra = getattr(pconfig, "extra", {}) or {}
    api_url = (
        extra.get("api_url") or os.getenv("FMSG_API_URL", DEFAULT_API_URL) or DEFAULT_API_URL
    ).strip().rstrip("/")
    api_key = (extra.get("api_key") or os.getenv("FMSG_API_KEY", "")).strip()
    if not api_key:
        return {"error": "fmsg standalone send: FMSG_API_KEY not configured"}
    if not chat_id:
        return {"error": "fmsg standalone send: no recipient address"}

    topic = (
        extra.get("default_topic")
        or os.getenv("FMSG_DEFAULT_TOPIC", "").strip()
        or DEFAULT_TOPIC
    )
    client = FmsgClient(TokenManager(api_url, api_key))
    try:
        await client.tokens.get_token()
        from_addr = client.tokens.address
        pid: Optional[int] = None
        if thread_id:
            try:
                pid = int(thread_id)
            except (TypeError, ValueError):
                pid = None

        # No explicit thread: continue the latest 1:1 DM with chat_id so
        # cron / home-channel delivery stays in one conversation.
        if pid is None:
            best: Optional[int] = None
            try:
                for msg in list(await client.list_messages(limit=50) or []) + list(
                    await client.list_sent(limit=50) or []
                ):
                    if not isinstance(msg, dict) or msg.get("id") is None:
                        continue
                    if not is_dm_with(msg, chat_id, from_addr or ""):
                        continue
                    mid = int(msg["id"])
                    if best is None or mid > best:
                        best = mid
            except (FmsgApiError, FmsgAuthError, TypeError, ValueError) as e:
                logger.debug("fmsg standalone send: last-DM lookup failed: %s", e)
            if best is not None:
                pid = best

        recipients = [chat_id]
        if pid is not None:
            try:
                parent = await client.get_message(pid)
                filtered = filter_recipients(
                    participants_from_msg(parent),
                    own_address=from_addr or "",
                    fallback=chat_id,
                )
                if filtered:
                    recipients = filtered
            except (FmsgApiError, FmsgAuthError) as e:
                logger.debug("fmsg standalone send: participant lookup failed: %s", e)

        draft_id = await client.create_draft(
            from_addr,
            recipients,
            message[:MAX_MESSAGE_LENGTH],
            pid=pid,
            topic=None if pid else topic,
        )
        for media_path in media_files or []:
            path = Path(media_path)
            try:
                data = path.read_bytes()
            except OSError as e:
                return {"error": f"fmsg standalone send: cannot read {media_path}: {e}"}
            if len(data) > MAX_ATTACH_BYTES:
                return {"error": f"fmsg standalone send: {path.name} exceeds attachment limit"}
            await client.attach(draft_id, path.name, data)
        await client.send(draft_id)
        return {
            "success": True,
            "platform": "fmsg",
            "chat_id": chat_id,
            "message_id": str(draft_id),
            "recipients": recipients,
        }
    except (FmsgAuthError, FmsgApiError) as e:
        return {"error": f"fmsg standalone send failed: {e}"}
    except Exception as e:
        return {"error": f"fmsg standalone send failed: {e}"}
    finally:
        await client.aclose()


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name="fmsg",
        label="fmsg",
        adapter_factory=lambda cfg: FmsgAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["FMSG_API_URL", "FMSG_API_KEY"],
        install_hint="pip install httpx websockets",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="FMSG_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="FMSG_ALLOWED_USERS",
        allow_all_env="FMSG_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="📨",
        platform_hint=(
            "You are communicating over fmsg, a federated messaging protocol. "
            "Messages are plain text (markdown renders client-dependent); files "
            "travel as attachments. Conversations are threaded: a root message "
            "carries a topic and replies chain to their parent. Addresses look "
            "like @user@example.com. Replies default to all participants of the "
            "message you are replying to (reply-all on that parent). Only omit "
            "someone in exceptional cases (e.g. privately warning others about "
            "malicious behaviour); normal group answers should keep everyone. "
            "When you send multiple messages in one turn they chain (each replies "
            "to the previous), not all to the same user prompt. The authenticated "
            "sender address comes from the API key's exchanged JWT; never ask for "
            "or invent a separate from address. Reply normally in the current "
            "conversation; Hermes will preserve its fmsg thread and recipients. "
            "To send to the configured home address, use the send_message tool with "
            "target `fmsg`. Do not call the fmsg Web API, read FMSG_API_KEY, construct "
            "drafts, or manage JWTs yourself; the platform adapter owns those tasks. "
            "Hermes Agent 0.18.x cannot parse an arbitrary fmsg address such as "
            "fmsg:@alice@example.com as an explicit send target. Report that routing "
            "limitation instead of bypassing the adapter. Deliver files through "
            "Hermes's normal attachment mechanism."
        ),
    )
