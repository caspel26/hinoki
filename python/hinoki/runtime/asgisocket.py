"""WebSocket over ASGI, for servers other than Kiso (Granian, test drivers).

Kiso does the framing, limits, Origin check and timers in Rust. Here the ASGI
server owns the frames, so only what a Python layer can still enforce is
enforced: the message size, the Origin and the handler lifecycle. Ping and
idle timers belong to the ASGI server."""

from __future__ import annotations

from typing import Any

from hinoki import core
from hinoki.runtime.requests import Scope
from hinoki.websocket import CloseCode, WebSocketRoute


class ASGITransport:
    """The ``Transport`` of one ASGI websocket connection."""

    def __init__(self, receive: Any, send: Any, max_message: int) -> None:
        self._receive = receive
        self._send = send
        self._max = max_message
        self._closed: tuple[int, str] | None = None

    @property
    def closed(self) -> tuple[int, str] | None:
        return self._closed

    async def receive(self) -> tuple[str, str | bytes] | None:
        if self._closed is not None:
            return None
        event = await self._receive()
        if event["type"] != "websocket.receive":
            self._closed = (int(event.get("code", CloseCode.GOING_AWAY)), "")
            return None
        text, data = event.get("text"), event.get("bytes") or b""
        size = len(text.encode()) if text is not None else len(data)
        if size > self._max:
            await self.close(int(CloseCode.MESSAGE_TOO_BIG), "message too big")
            return None
        return ("bytes", data) if text is None else ("text", text)

    async def _emit(self, event: dict[str, Any]) -> bool:
        if self._closed is not None:
            return False
        try:
            await self._send(event)
        except Exception:
            self._closed = (int(CloseCode.GOING_AWAY), "")
            return False
        return True

    async def send_text(self, text: str) -> bool:
        return await self._emit({"type": "websocket.send", "text": text})

    async def send_bytes(self, data: bytes) -> bool:
        return await self._emit({"type": "websocket.send", "bytes": data})

    async def close(self, code: int = 1000, reason: str = "") -> bool:
        sent = await self._emit(
            {"type": "websocket.close", "code": code, "reason": reason}
        )
        if sent:
            self._closed = (code, reason)
        return sent


# ``wss`` from the server, ``https`` from a trusted proxy's X-Forwarded-Proto
# (``effective_scope`` writes that over the socket scheme).
TLS_SCHEMES = ("wss", "https")


def origin_allowed(
    route: WebSocketRoute, scope: Scope, cors_origins: frozenset[str] = frozenset()
) -> bool:
    """The Origin rule of the native lane, called rather than copied: an absent
    Origin (not a browser) passes; otherwise it must be one the route names, one
    the application's CORS policy lists, or the scheme the connection has
    (``wss`` or a proxy-reported ``https`` is TLS, ``ws`` is http) and the Host
    the request was addressed to. The CORS wildcard is not honoured: a
    handshake carries cookies and a browser does not hide its answer. The route
    and CORS origins are lower case already (registration and policy build), so
    they are passed as is."""
    headers = {k.decode("latin-1").lower(): v for k, v in scope.get("headers", [])}
    origin = headers.get("origin")
    host = headers.get("host")
    return core.websocket_origin_allowed(
        None if origin is None else origin.decode("latin-1"),
        None if host is None else host.decode("latin-1"),
        scope.get("scheme") in TLS_SCHEMES,
        list(route.origins),
        list(cors_origins),
    )


async def connected(receive: Any) -> bool:
    """Read the ``websocket.connect`` event that opens an ASGI WebSocket; False
    when the client left first."""
    event = await receive()
    return bool(event["type"] == "websocket.connect")


async def accept(send: Any, subprotocol: str | None) -> None:
    """Accept a handshake whose ``websocket.connect`` was read."""
    await send({"type": "websocket.accept", "subprotocol": subprotocol})


async def refuse(send: Any) -> None:
    """Refuse before accepting: an ASGI server answers this with HTTP 403."""
    await send({"type": "websocket.close", "code": int(CloseCode.POLICY_VIOLATION)})
