"""WebSocket actions through the ASGI adapter (Granian and in-process drivers)."""

from __future__ import annotations

import asyncio
from typing import Any

from hinoki import Hinoki, http
from hinoki.runtime.asgi import ASGIAdapter


@http.websocket("/echo/{name}", origins=["https://app.example"], max_message_bytes=8)
async def echo(name: str, socket: http.WebSocket) -> None:
    async for message in socket:
        await socket.send_text(f"{name}:{message}")


@http.websocket("/boom")
async def boom(socket: http.WebSocket) -> None:
    raise RuntimeError("handler failed")


class Peer:
    """The server side of an ASGI websocket connection, scripted by the test."""

    def __init__(self, path: str, headers: list[tuple[bytes, bytes]]) -> None:
        self.scope = {
            "type": "websocket",
            "path": path,
            "query_string": b"",
            "headers": [(b"host", b"testserver"), *headers],
            "subprotocols": [],
        }
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []

    async def receive(self) -> dict[str, Any]:
        return await self.inbox.get()

    async def send(self, event: dict[str, Any]) -> None:
        self.sent.append(event)


async def run(peer: Peer, *incoming: dict[str, Any]) -> list[dict[str, Any]]:
    app = Hinoki()
    app.include(echo, boom)
    adapter = ASGIAdapter(app)
    for event in incoming:
        peer.inbox.put_nowait(event)
    await asyncio.wait_for(adapter(peer.scope, peer.receive, peer.send), 5)
    return peer.sent


def connect() -> dict[str, Any]:
    return {"type": "websocket.connect"}


def text(value: str) -> dict[str, Any]:
    return {"type": "websocket.receive", "text": value}


async def test_messages_flow_and_the_handler_closes_normally() -> None:
    sent = await run(
        Peer("/echo/a", []),
        connect(),
        text("hi"),
        {"type": "websocket.disconnect", "code": 1000},
    )
    assert sent[0]["type"] == "websocket.accept"
    assert sent[1] == {"type": "websocket.send", "text": "a:hi"}


async def test_an_unknown_path_is_refused_before_accept() -> None:
    sent = await run(Peer("/nowhere", []), connect())
    assert [event["type"] for event in sent] == ["websocket.close"]


async def test_a_foreign_origin_is_refused_and_a_listed_one_accepted() -> None:
    refused = await run(
        Peer("/echo/a", [(b"origin", b"https://evil.example")]), connect()
    )
    assert [event["type"] for event in refused] == ["websocket.close"]
    accepted = await run(
        Peer("/echo/a", [(b"origin", b"https://app.example")]),
        connect(),
        {"type": "websocket.disconnect", "code": 1000},
    )
    assert accepted[0]["type"] == "websocket.accept"


async def test_a_message_over_the_limit_closes_with_1009() -> None:
    sent = await run(Peer("/echo/a", []), connect(), text("x" * 9))
    assert sent[-1]["type"] == "websocket.close"
    assert sent[-1]["code"] == 1009


async def test_a_failing_handler_closes_with_1011() -> None:
    sent = await run(Peer("/boom", []), connect())
    assert sent[-1]["code"] == 1011
