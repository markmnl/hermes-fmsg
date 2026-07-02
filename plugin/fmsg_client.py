"""Async client for fmsg-webapi.

Gateway-independent: this module has no Hermes imports so it can be
unit-tested (and reused) on its own. The adapter in ``adapter.py`` builds
on top of it.

Auth model mirrors fmsg-cli (`internal/auth/manager.go`): an opaque API
key of the form ``fmsgk_<key_id>_<secret>`` is exchanged at
``POST /fmsg/token`` for a short-lived Ed25519 JWT (default TTL 12h).
The JWT is cached and re-exchanged within 5 minutes of expiry; any 401
on a protected route forces one re-exchange and retry.
"""

import asyncio
import base64
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# Re-exchange the API key this long before the JWT's expiry
# (same window as fmsg-cli's refreshBefore).
TOKEN_REFRESH_BEFORE = timedelta(minutes=5)


class FmsgAuthError(Exception):
    """API-key exchange was rejected (bad/revoked/expired key, CIDR block)."""


class FmsgApiError(Exception):
    """A non-2xx response from fmsg-webapi."""

    def __init__(self, status_code: int, detail: str = ""):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"fmsg-webapi HTTP {status_code}: {detail}")


def decode_jwt_claims(token: str) -> Dict[str, Any]:
    """Decode a JWT payload without verifying the signature.

    The client only needs the ``sub`` claim (the granted fmsg address);
    the server is the party that verifies signatures.
    """
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except (IndexError, ValueError, json.JSONDecodeError) as e:
        raise ValueError(f"malformed JWT: {e}") from e


def _parse_expires_at(raw: str) -> datetime:
    """Parse the RFC 3339 ``expires_at`` from the token response."""
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


class TokenManager:
    """Exchanges an fmsg API key for a JWT and caches it until near expiry."""

    def __init__(self, api_url: str, api_key: str, http: Optional[httpx.AsyncClient] = None):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key.strip()
        self._http = http
        self._owns_http = http is None
        self._token: Optional[str] = None
        self._expires_at: Optional[datetime] = None
        self._address: Optional[str] = None
        self._lock = asyncio.Lock()

    @property
    def address(self) -> Optional[str]:
        """The granted fmsg address (JWT ``sub``); None before first exchange."""
        return self._address

    @property
    def expires_at(self) -> Optional[datetime]:
        return self._expires_at

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30.0)
        return self._http

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    def _cached_valid(self) -> bool:
        return (
            self._token is not None
            and self._expires_at is not None
            and datetime.now(timezone.utc) + TOKEN_REFRESH_BEFORE < self._expires_at
        )

    async def get_token(self, force: bool = False) -> str:
        async with self._lock:
            if not force and self._cached_valid():
                return self._token
            resp = await self._client().post(
                f"{self.api_url}/fmsg/token",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            if resp.status_code in (401, 403):
                raise FmsgAuthError(
                    f"API key rejected (HTTP {resp.status_code}): {resp.text[:200]}"
                )
            if resp.status_code >= 300:
                raise FmsgApiError(resp.status_code, resp.text[:200])
            body = resp.json()
            self._token = body["access_token"]
            self._expires_at = _parse_expires_at(body["expires_at"])
            self._address = decode_jwt_claims(self._token).get("sub")
            logger.debug(
                "fmsg token exchanged for %s, expires %s", self._address, self._expires_at
            )
            return self._token


class FmsgClient:
    """Thin async wrapper over fmsg-webapi's REST routes."""

    def __init__(self, tokens: TokenManager, http: Optional[httpx.AsyncClient] = None):
        self.tokens = tokens
        self.api_url = tokens.api_url
        self._http = http
        self._owns_http = http is None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=60.0)
        return self._http

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None
        await self.tokens.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Authenticated request; one forced token refresh + retry on 401."""
        token = await self.tokens.get_token()
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {token}"
        resp = await self._client().request(method, f"{self.api_url}{path}", headers=headers, **kwargs)
        if resp.status_code == 401:
            token = await self.tokens.get_token(force=True)
            headers["Authorization"] = f"Bearer {token}"
            resp = await self._client().request(
                method, f"{self.api_url}{path}", headers=headers, **kwargs
            )
        if resp.status_code >= 300:
            raise FmsgApiError(resp.status_code, resp.text[:200])
        return resp

    # -- Messages -------------------------------------------------------------

    async def list_messages(self, limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
        resp = await self._request("GET", "/fmsg", params={"limit": limit, "offset": offset})
        return resp.json() or []

    async def get_message(self, msg_id: int) -> Dict[str, Any]:
        resp = await self._request("GET", f"/fmsg/{msg_id}")
        return resp.json()

    async def get_data(self, msg_id: int) -> bytes:
        resp = await self._request("GET", f"/fmsg/{msg_id}/data")
        return resp.content

    async def create_draft(
        self,
        from_addr: str,
        to: List[str],
        data: str,
        *,
        pid: Optional[int] = None,
        topic: Optional[str] = None,
        mime: str = "text/plain; charset=utf-8",
        important: bool = False,
        no_reply: bool = False,
    ) -> int:
        # The webapi stores `data` verbatim as the message body — it must be
        # a UTF-8 string. Binary payloads go as attachments, never as body.
        if pid is not None and topic is not None:
            raise ValueError("pid and topic are mutually exclusive")
        body: Dict[str, Any] = {
            "version": 1,
            "from": from_addr,
            "to": to,
            "type": mime,
            "size": len(data.encode("utf-8")),
            "data": data,
        }
        if pid is not None:
            body["pid"] = pid
        if topic is not None:
            body["topic"] = topic
        if important:
            body["important"] = True
        if no_reply:
            body["no_reply"] = True
        resp = await self._request("POST", "/fmsg", json=body)
        return resp.json()["id"]

    async def send(self, msg_id: int) -> Dict[str, Any]:
        resp = await self._request("POST", f"/fmsg/{msg_id}/send")
        return resp.json()

    async def delete_draft(self, msg_id: int) -> None:
        await self._request("DELETE", f"/fmsg/{msg_id}")

    async def mark_read(self, msg_id: int) -> Dict[str, Any]:
        resp = await self._request("POST", f"/fmsg/{msg_id}/read")
        return resp.json()

    # -- Attachments ------------------------------------------------------------

    async def attach(self, msg_id: int, filename: str, content: bytes) -> Dict[str, Any]:
        resp = await self._request(
            "POST",
            f"/fmsg/{msg_id}/attach",
            files={"file": (filename, content)},
        )
        return resp.json()

    async def get_attachment(self, msg_id: int, filename: str) -> bytes:
        resp = await self._request("GET", f"/fmsg/{msg_id}/attach/{filename}")
        return resp.content

    # -- WebSocket ---------------------------------------------------------------

    def ws_url(self) -> str:
        base = self.api_url
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        return f"{base}/fmsg/ws"

    async def iter_events(self) -> AsyncIterator[Dict[str, Any]]:
        """Connect to /fmsg/ws and yield decoded event envelopes.

        Yields dicts like ``{"type": "new_msg", "data": {...}}``. Runs until
        the connection drops (raises) or the caller cancels. The token used
        for the handshake is whatever TokenManager currently holds — callers
        handle reconnect and token rotation.
        """
        import websockets

        token = await self.tokens.get_token()
        headers = {"Authorization": f"Bearer {token}"}
        try:
            conn = websockets.connect(self.ws_url(), additional_headers=headers)
        except TypeError:
            # websockets < 13 (legacy client) uses extra_headers
            conn = websockets.connect(self.ws_url(), extra_headers=headers)
        async with conn as ws:
            async for frame in ws:
                try:
                    event = json.loads(frame)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(event, dict):
                    yield event
