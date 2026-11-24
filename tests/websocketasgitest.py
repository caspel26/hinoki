"""WebSocket actions through the ASGI adapter (Granian and in-process drivers)."""

from __future__ import annotations

import asyncio
from typing import Any

from hinoki import API, Access, BearerAuth, Caller, Hinoki, http
from hinoki.runtime.asgi import ASGIAdapter
from hinoki.settings import AppSettings


@http.websocket("/echo/{name}", origins=["https://app.example"], max_message_bytes=8)
async def echo(name: str, socket: http.WebSocket) -> None:
    async for message in socket:
        await socket.send_text(f"{name}:{message}")


class Nobody(BearerAuth[str]):
    def resolve_token(self, token: str) -> Caller[str] | None:
        return None


@http.websocket("/private", access=Access(Nobody()))
async def private(socket: http.WebSocket) -> None:
    raise AssertionError("an unauthenticated client must not reach the handler")


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


async def run(
    peer: Peer, *incoming: dict[str, Any], settings: AppSettings | None = None
) -> list[dict[str, Any]]:
    app = Hinoki(settings=settings) if settings else Hinoki()
    app.include(echo, boom, private)
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


async def test_a_double_encoded_path_does_not_reach_another_route() -> None:
    peer = Peer("/echo/..%2fprivate", [])
    peer.scope["raw_path"] = b"/echo/..%252fprivate"
    peer.scope["path"] = "/echo/..%2fprivate"
    sent = await run(peer, connect(), {"type": "websocket.disconnect", "code": 1000})
    assert sent[0]["type"] == "websocket.accept"
    assert sent[1:] == []  # the handler saw one segment, not two


async def test_the_origin_must_match_the_scheme_of_the_connection() -> None:
    plain = Peer("/echo/a", [(b"origin", b"http://testserver")])
    plain.scope["scheme"] = "wss"
    assert [e["type"] for e in await run(plain, connect())] == ["websocket.close"]
    secure = Peer("/echo/a", [(b"origin", b"https://testserver")])
    secure.scope["scheme"] = "wss"
    sent = await run(secure, connect(), {"type": "websocket.disconnect", "code": 1000})
    assert sent[0]["type"] == "websocket.accept"


async def test_a_host_the_application_does_not_allow_is_refused() -> None:
    sent = await run(
        Peer("/echo/a", []),
        connect(),
        settings=AppSettings(allowed_hosts=("api.example.com",)),
    )
    assert [event["type"] for event in sent] == ["websocket.close"]


async def test_a_message_over_the_limit_closes_with_1009() -> None:
    sent = await run(Peer("/echo/a", []), connect(), text("x" * 9))
    assert sent[-1]["type"] == "websocket.close"
    assert sent[-1]["code"] == 1009


async def test_a_failing_handler_closes_with_1011() -> None:
    sent = await run(Peer("/boom", []), connect())
    assert sent[-1]["code"] == 1011


async def test_an_unauthenticated_client_is_refused_before_accept() -> None:
    sent = await run(Peer("/private", []), connect())
    assert [event["type"] for event in sent] == ["websocket.close"]
    assert sent[0]["code"] == 1008


async def test_a_client_that_leaves_before_connecting_gets_nothing() -> None:
    sent = await run(Peer("/echo/a", []), {"type": "websocket.disconnect"})
    assert sent == []


async def test_a_refusal_is_sent_only_after_the_connect_event_is_read() -> None:
    order: list[str] = []
    peer = Peer("/nowhere", [])
    peer.inbox.put_nowait(connect())

    async def receive() -> dict[str, Any]:
        order.append("receive")
        return await peer.inbox.get()

    async def send(event: dict[str, Any]) -> None:
        order.append(event["type"])

    await asyncio.wait_for(ASGIAdapter(Hinoki())(peer.scope, receive, send), 5)
    assert order == ["receive", "websocket.close"]


async def test_a_client_gone_before_connecting_to_an_unknown_path_gets_nothing() -> (
    None
):
    assert await run(Peer("/nowhere", []), {"type": "websocket.disconnect"}) == []


class Rooms(API, path="/rooms"):
    greeting = "hello"

    @http.websocket("/{room}")
    async def join(self, room: str, socket: http.WebSocket, size: Later) -> None:
        await socket.send_text(f"{self.greeting} {room} {size + 1}")


Later = int  # defined after the handler that names it: read at include time


async def test_an_api_class_declares_websockets_under_its_prefix() -> None:
    peer = Peer("/rooms/lobby", [])
    peer.scope["query_string"] = b"size=3"
    app = Hinoki(include=[Rooms])
    for event in (connect(), {"type": "websocket.disconnect", "code": 1000}):
        peer.inbox.put_nowait(event)
    await asyncio.wait_for(ASGIAdapter(app)(peer.scope, peer.receive, peer.send), 5)
    assert peer.sent[0]["type"] == "websocket.accept"
    assert peer.sent[1] == {"type": "websocket.send", "text": "hello lobby 4"}
