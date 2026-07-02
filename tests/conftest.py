"""Test fixtures.

Two pieces of scaffolding:

- A minimal stub of the ``gateway`` package (Platform, PlatformConfig,
  BasePlatformAdapter, MessageEvent, ...) so ``plugin.adapter`` imports
  without a hermes-agent checkout. The stub mirrors only the surface the
  adapter uses; signatures match hermes-agent's gateway/platforms/base.py.
- ``FakeWebAPI``: an in-memory fmsg-webapi implemented as an
  ``httpx.MockTransport`` handler — token exchange, drafts, send, read,
  attachments, inbox listing.
"""

import base64
import json
import sys
import time
import types
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# gateway stub
# ---------------------------------------------------------------------------

def _install_gateway_stub(media_dir: Path) -> None:
    if "gateway" in sys.modules:
        return

    gateway = types.ModuleType("gateway")
    config_mod = types.ModuleType("gateway.config")
    platforms_mod = types.ModuleType("gateway.platforms")
    base_mod = types.ModuleType("gateway.platforms.base")

    class Platform(Enum):
        LOCAL = "local"

        @classmethod
        def _missing_(cls, value):
            pseudo = object.__new__(cls)
            pseudo._value_ = value
            pseudo._name_ = str(value).upper()
            cls._value2member_map_[value] = pseudo
            return pseudo

    @dataclass
    class PlatformConfig:
        enabled: bool = False
        extra: Dict[str, Any] = field(default_factory=dict)

    class MessageType(Enum):
        TEXT = "text"
        PHOTO = "photo"
        VIDEO = "video"
        AUDIO = "audio"
        VOICE = "voice"
        DOCUMENT = "document"

    @dataclass
    class SessionSource:
        platform: Any = None
        chat_id: str = ""
        chat_name: Optional[str] = None
        chat_type: str = "dm"
        user_id: Optional[str] = None
        user_name: Optional[str] = None
        thread_id: Optional[str] = None
        chat_topic: Optional[str] = None
        message_id: Optional[str] = None

    @dataclass
    class MessageEvent:
        text: str
        message_type: MessageType = MessageType.TEXT
        source: Any = None
        raw_message: Any = None
        message_id: Optional[str] = None
        media_urls: List[str] = field(default_factory=list)
        media_types: List[str] = field(default_factory=list)
        reply_to_message_id: Optional[str] = None
        channel_context: Optional[str] = None
        metadata: Dict[str, Any] = field(default_factory=dict)
        timestamp: datetime = field(default_factory=datetime.now)

    @dataclass
    class SendResult:
        success: bool
        message_id: Optional[str] = None
        error: Optional[str] = None
        raw_response: Any = None
        retryable: bool = False
        error_kind: Optional[str] = None

    @dataclass
    class CachedMedia:
        path: str
        media_type: str
        kind: str
        display_name: str

    def cache_media_bytes(data, *, filename="", mime_type="", default_kind=None):
        mime = (mime_type or "").lower()
        if mime.startswith("image/"):
            kind = "image"
        elif mime.startswith("video/"):
            kind = "video"
        elif mime.startswith("audio/"):
            kind = "audio"
        else:
            kind = "document"
        out = media_dir / f"{time.monotonic_ns()}-{filename or 'file.bin'}"
        out.write_bytes(data)
        return CachedMedia(str(out), mime or "application/octet-stream", kind, filename)

    def get_inbound_media_max_bytes() -> int:
        return 10 * 1024 * 1024

    class BasePlatformAdapter:
        supports_code_blocks = False
        splits_long_messages = False

        def __init__(self, config, platform):
            self.config = config
            self.platform = platform
            self.name = platform.value
            self._running = False
            self.handled_events: List[MessageEvent] = []
            self.fatal_error: Optional[tuple] = None

        def _mark_connected(self):
            self._running = True

        def _mark_disconnected(self):
            self._running = False

        def _set_fatal_error(self, code, message, *, retryable):
            self._running = False
            self.fatal_error = (code, message, retryable)

        def build_source(self, chat_id, chat_name=None, chat_type="dm",
                         user_id=None, user_name=None, thread_id=None,
                         chat_topic=None, message_id=None, **kwargs):
            if chat_topic is not None and not chat_topic.strip():
                chat_topic = None
            return SessionSource(
                platform=self.platform, chat_id=str(chat_id), chat_name=chat_name,
                chat_type=chat_type, user_id=str(user_id) if user_id else None,
                user_name=user_name,
                thread_id=str(thread_id) if thread_id else None,
                chat_topic=chat_topic, message_id=message_id,
            )

        async def handle_message(self, event):
            self.handled_events.append(event)

    config_mod.Platform = Platform
    config_mod.PlatformConfig = PlatformConfig
    base_mod.BasePlatformAdapter = BasePlatformAdapter
    base_mod.MessageEvent = MessageEvent
    base_mod.MessageType = MessageType
    base_mod.SendResult = SendResult
    base_mod.SessionSource = SessionSource
    base_mod.CachedMedia = CachedMedia
    base_mod.cache_media_bytes = cache_media_bytes
    base_mod.get_inbound_media_max_bytes = get_inbound_media_max_bytes

    gateway.config = config_mod
    gateway.platforms = platforms_mod
    platforms_mod.base = base_mod
    sys.modules["gateway"] = gateway
    sys.modules["gateway.config"] = config_mod
    sys.modules["gateway.platforms"] = platforms_mod
    sys.modules["gateway.platforms.base"] = base_mod


_MEDIA_DIR = Path(__file__).parent / ".media-cache"
_MEDIA_DIR.mkdir(exist_ok=True)
_install_gateway_stub(_MEDIA_DIR)


# ---------------------------------------------------------------------------
# Fake fmsg-webapi
# ---------------------------------------------------------------------------

API_KEY = "fmsgk_testkey_secret"
BOT_ADDRESS = "@alice_hermes@example.com"
TOKEN_TTL_SECONDS = 12 * 3600


def make_jwt(sub: str, exp_epoch: float) -> str:
    def b64(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'EdDSA'})}.{b64({'sub': sub, 'exp': int(exp_epoch)})}.sig"


class FakeWebAPI:
    """In-memory fmsg-webapi behind an httpx.MockTransport."""

    def __init__(self, api_key: str = API_KEY, address: str = BOT_ADDRESS):
        self.api_key = api_key
        self.address = address
        self.messages: Dict[int, Dict[str, Any]] = {}
        self.attachments: Dict[int, Dict[str, bytes]] = {}
        self.next_id = 1
        self.token_exchanges = 0
        self.issued_tokens: List[str] = []
        self.reject_next_requests = 0  # force 401s on protected routes
        self.calls: List[str] = []  # "METHOD /path" log for ordering assertions

    # -- Server-side helpers ---------------------------------------------------

    def seed_message(
        self,
        from_addr: str,
        to: List[str],
        data: str = "",
        *,
        pid: Optional[int] = None,
        topic: Optional[str] = None,
        mime: str = "text/plain; charset=utf-8",
        sent: bool = True,
        read: bool = False,
        attachments: Optional[Dict[str, bytes]] = None,
        important: bool = False,
        no_reply: bool = False,
    ) -> int:
        msg_id = self.next_id
        self.next_id += 1
        self.messages[msg_id] = {
            "id": msg_id,
            "version": 1,
            "has_pid": pid is not None,
            "pid": pid,
            "from": from_addr,
            "to": to,
            "time": time.time() if sent else None,
            "topic": topic,
            "type": mime,
            "size": len(data.encode("utf-8")),
            "important": important,
            "no_reply": no_reply,
            "read": read,
            "time_read": None,
            "attachments": [
                {"filename": name, "size": len(blob)}
                for name, blob in (attachments or {}).items()
            ],
            "_data": data,
            "_sent": sent,
        }
        self.attachments[msg_id] = dict(attachments or {})
        return msg_id

    def _public(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        out = {k: v for k, v in msg.items() if not k.startswith("_")}
        data = msg["_data"]
        if msg["type"].startswith("text/"):
            out["short_text"] = data[:768]
        return out

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    # -- Request handling -------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        self.calls.append(f"{method} {path}")

        if method == "POST" and path == "/fmsg/token":
            auth = request.headers.get("Authorization", "")
            if auth != f"Bearer {self.api_key}":
                return httpx.Response(401, text="invalid api key")
            self.token_exchanges += 1
            exp = time.time() + TOKEN_TTL_SECONDS
            token = make_jwt(self.address, exp)
            self.issued_tokens.append(token)
            from datetime import datetime, timezone
            return httpx.Response(200, json={
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": TOKEN_TTL_SECONDS,
                "expires_at": datetime.fromtimestamp(exp, tz=timezone.utc)
                .isoformat().replace("+00:00", "Z"),
            })

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth.removeprefix("Bearer ") not in self.issued_tokens:
            return httpx.Response(401, text="unauthorized")
        if self.reject_next_requests > 0:
            self.reject_next_requests -= 1
            return httpx.Response(401, text="token revoked")

        parts = [p for p in path.split("/") if p]  # ["fmsg", ...]

        if method == "GET" and path == "/fmsg":
            limit = int(request.url.params.get("limit", 20))
            offset = int(request.url.params.get("offset", 0))
            inbox = [
                self._public(m)
                for m in sorted(self.messages.values(), key=lambda m: -m["id"])
                if self.address in m["to"] and m["_sent"]
            ]
            return httpx.Response(200, json=inbox[offset:offset + limit])

        if method == "POST" and path == "/fmsg":
            body = json.loads(request.content)
            if body.get("pid") is not None and body.get("topic") is not None:
                return httpx.Response(400, text="topic set together with pid")
            if body.get("from") != self.address:
                return httpx.Response(403, text="from does not match identity")
            msg_id = self.seed_message(
                body["from"], body["to"], body.get("data", ""),
                pid=body.get("pid"), topic=body.get("topic"),
                mime=body["type"], sent=False,
                important=body.get("important", False),
                no_reply=body.get("no_reply", False),
            )
            return httpx.Response(201, json={"id": msg_id})

        if len(parts) >= 2 and parts[0] == "fmsg" and parts[1].isdigit():
            msg_id = int(parts[1])
            msg = self.messages.get(msg_id)
            if msg is None:
                return httpx.Response(404, text="not found")

            if len(parts) == 2 and method == "GET":
                return httpx.Response(200, json=self._public(msg))
            if len(parts) == 2 and method == "DELETE":
                if msg["_sent"]:
                    return httpx.Response(403, text="already sent")
                del self.messages[msg_id]
                return httpx.Response(204)
            if len(parts) == 3 and parts[2] == "send" and method == "POST":
                if msg["_sent"]:
                    return httpx.Response(409, text="already sent")
                msg["_sent"] = True
                msg["time"] = time.time()
                return httpx.Response(200, json={"id": msg_id, "time": msg["time"]})
            if len(parts) == 3 and parts[2] == "read" and method == "POST":
                msg["read"] = True
                msg["time_read"] = msg["time_read"] or time.time()
                return httpx.Response(200, json={"id": msg_id, "time_read": msg["time_read"]})
            if len(parts) == 3 and parts[2] == "data" and method == "GET":
                return httpx.Response(200, content=msg["_data"].encode("utf-8"))
            if len(parts) == 3 and parts[2] == "attach" and method == "POST":
                if msg["_sent"]:
                    return httpx.Response(403, text="already sent")
                # Minimal multipart parse: filename= and payload between CRLFCRLF and boundary.
                content = request.content
                header, _, rest = content.partition(b"\r\n\r\n")
                filename = header.split(b'filename="')[1].split(b'"')[0].decode()
                boundary = content.split(b"\r\n", 1)[0]
                payload = rest.rsplit(b"\r\n" + boundary, 1)[0]
                self.attachments[msg_id][filename] = payload
                msg["attachments"].append({"filename": filename, "size": len(payload)})
                return httpx.Response(201, json={"filename": filename, "size": len(payload)})
            if len(parts) == 4 and parts[2] == "attach" and method == "GET":
                blob = self.attachments[msg_id].get(parts[3])
                if blob is None:
                    return httpx.Response(404, text="no such attachment")
                return httpx.Response(200, content=blob)

        return httpx.Response(404, text=f"unhandled {method} {path}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_api() -> FakeWebAPI:
    return FakeWebAPI()


@pytest.fixture
def http_client(fake_api) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=fake_api.transport(), base_url="http://fmsg.test")


@pytest.fixture
def token_manager(fake_api, http_client):
    from plugin.fmsg_client import TokenManager

    return TokenManager("http://fmsg.test", API_KEY, http=http_client)


@pytest.fixture
def client(token_manager, http_client):
    from plugin.fmsg_client import FmsgClient

    return FmsgClient(token_manager, http=http_client)


@pytest.fixture
def adapter(fake_api, client, token_manager, tmp_path, monkeypatch):
    """A wired FmsgAdapter with client/tokens injected (no real connect())."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.config import PlatformConfig
    from plugin.adapter import FmsgAdapter

    config = PlatformConfig(
        enabled=True,
        extra={"api_url": "http://fmsg.test", "api_key": API_KEY},
    )
    a = FmsgAdapter(config)
    a._tokens = token_manager
    a._client = client
    a._running = True
    a._state_path = tmp_path / "fmsg_last_seen.json"
    return a
