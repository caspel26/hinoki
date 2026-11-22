"""WebSocket over ASGI, for servers other than Kiso (Granian, test drivers).

Kiso does the framing, limits, Origin check and timers in Rust. Here the ASGI
server owns the frames, so only what a Python layer can still enforce is
enforced: the message size, the Origin and the handler lifecycle. Ping and
idle timers belong to the ASGI server."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

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


def origin_allowed(route: WebSocketRoute, scope: Scope) -> bool:
    """An absent Origin (not a browser) passes; otherwise it must be one the
    route names, ``*``, or the host the request was addressed to."""
    headers = {k.decode("latin-1").lower(): v for k, v in scope.get("headers", [])}
    origin = headers.get("origin")
    if origin is None:
        return True
    value = origin.decode("latin-1")
    if "*" in route.origins or value in route.origins:
        return True
    host = headers.get("host", b"").decode("latin-1")
    return bool(host) and urlsplit(value).netloc == host


async def accept(receive: Any, send: Any, subprotocol: str | None) -> bool:
    """Complete the ASGI connect handshake; False when the client left."""
    event = await receive()
    if event["type"] != "websocket.connect":
        return False
    await send({"type": "websocket.accept", "subprotocol": subprotocol})
    return True


async def refuse(send: Any) -> None:
    """Refuse before accepting: an ASGI server answers this with HTTP 403."""
    await send({"type": "websocket.close", "code": int(CloseCode.POLICY_VIOLATION)})
