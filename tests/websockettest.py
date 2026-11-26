"""WebSocket actions over real loopback sockets with the Rust server (Kiso)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import struct
import time
from typing import Any

import pytest

from hinoki import API, Access, Caller, Hinoki, Schema, core, http
from hinoki.crypto import ClaimsJWTAuth, JWTKey
from hinoki.errors import RegistrationError
from hinoki.runtime.kiso import KisoWorker
from hinoki.runtime.responses import HTTPError
from hinoki.runtime.websockets import Admitted, run, value_of
from hinoki.settings import AppSettings, CorsSettings, ExecutionSettings
from hinoki.websocket import Param, WebSocketClosed, WebSocketRoute, convert

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
SECRET = b"k" * 32
TEXT, BINARY, CLOSE, PING, PONG = 1, 2, 8, 9, 10


class Tokens(ClaimsJWTAuth):
    keys = (JWTKey(None, "HS256", SECRET),)
    issuer = "https://issuer.example"
    audience = "api"
    leeway = 5


def token(*scopes: str) -> str:
    claims = {
        "sub": "u1",
        "scopes": list(scopes),
        "iss": Tokens.issuer,
        "aud": Tokens.audience,
        "exp": int(time.time()) + 300,
    }
    return core.jwt_encode(core.encode_json(claims), (None, "HS256", SECRET, False))


class Move(Schema):
    x: int
    y: int


class Moved(Schema):
    x: int
    y: int
    by: str


@http.websocket("/echo")
async def echo(socket: http.WebSocket) -> None:
    async for message in socket:
        if isinstance(message, bytes):
            await socket.send_bytes(message)
        else:
            await socket.send_text(message)


@http.websocket("/rooms/{room}")
async def room(room: str, socket: http.WebSocket, size: int = 1) -> None:
    await socket.send_text(f"{room}:{size}")


@http.websocket("/typed", message=Move, reply=Moved)
async def typed(socket: http.WebSocket) -> None:
    async for move in socket:
        await socket.send_json({"x": move.x + 1, "y": move.y, "by": "server"})


@http.websocket("/private", access=Access(Tokens(), permissions=["chat:read"]))
async def private(who: Caller[None], socket: http.WebSocket) -> None:
    await socket.send_text(who.id)


@http.websocket(
    "/speak",
    subprotocols={"open": None, "secure": Access(Tokens(), permissions=["chat:read"])},
)
async def speak(socket: http.WebSocket) -> None:
    await socket.send_text(socket.subprotocol or "none")


@http.websocket("/small", max_message_bytes=16)
async def small(socket: http.WebSocket) -> None:
    async for message in socket:
        await socket.send_text(str(len(message)))


@http.websocket("/boom")
async def boom(socket: http.WebSocket) -> None:
    raise RuntimeError("handler failed")


@http.websocket("/browser", origins=["https://app.example"])
async def browser(socket: http.WebSocket) -> None:
    await socket.send_text("hi")


@http.get("/rooms/stats")
async def room_stats() -> dict[str, str]:
    return {"stats": "yes"}


def frame(opcode: int, payload: bytes = b"", *, fin: bool = True) -> bytes:
    """A masked client frame."""
    head = bytes([(0x80 if fin else 0) | opcode])
    size = len(payload)
    if size < 126:
        head += bytes([0x80 | size])
    elif size < 65536:
        head += bytes([0x80 | 126]) + struct.pack(">H", size)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", size)
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return head + mask + masked


class Client:
    """A synthetic WebSocket client: the handshake and frames by hand."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.status = 0
        self.headers: dict[str, str] = {}
        self.body = b""

    @classmethod
    async def open(
        cls,
        port: int,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        upgrade: bool = True,
        key: str | None = None,
    ) -> Client:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        key = key or base64.b64encode(os.urandom(16)).decode()
        lines = [f"GET {path} HTTP/1.1", f"host: 127.0.0.1:{port}"]
        if upgrade:
            lines += [
                "connection: upgrade",
                "upgrade: websocket",
                "sec-websocket-version: 13",
                f"sec-websocket-key: {key}",
            ]
        lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        client = cls(reader, writer)
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        rows = head.decode("latin-1").split("\r\n")
        client.status = int(rows[0].split()[1])
        client.headers = {
            k.lower(): v for k, v in (r.split(": ", 1) for r in rows[1:] if r)
        }
        if client.status == 101:
            expected = base64.b64encode(
                hashlib.sha1(key.encode() + GUID).digest()  # noqa: S324 - RFC 6455
            )
            assert client.headers["sec-websocket-accept"] == expected.decode()
        else:
            size = int(client.headers.get("content-length", "0"))
            client.body = await asyncio.wait_for(reader.readexactly(size), 5)
        return client

    def send(self, opcode: int, payload: bytes = b"", *, fin: bool = True) -> None:
        self.writer.write(frame(opcode, payload, fin=fin))

    async def receive(self, wait: float = 5) -> tuple[int, bytes]:
        head = await asyncio.wait_for(self.reader.readexactly(2), wait)
        size = head[1] & 0x7F
        if size == 126:
            size = struct.unpack(">H", await self.reader.readexactly(2))[0]
        elif size == 127:
            size = struct.unpack(">Q", await self.reader.readexactly(8))[0]
        return head[0] & 0x0F, await self.reader.readexactly(size)

    async def message(self) -> tuple[int, bytes]:
        """The next data message, answering nothing and skipping pings."""
        while True:
            opcode, payload = await self.receive()
            if opcode not in (PING, PONG):
                return opcode, payload

    async def closed_with(self) -> int:
        opcode, payload = await self.message()
        assert opcode == CLOSE
        return int(struct.unpack(">H", payload[:2])[0])

    def close(self) -> None:
        self.writer.close()


def app(cors: tuple[str, ...] = (), **execution: Any) -> Hinoki:
    settings = AppSettings(
        allowed_hosts=("127.0.0.1",),  # WebSocket apps refuse "*" outside debug
        execution=ExecutionSettings(**execution),
        cors=CorsSettings(origins=cors),
    )
    built = Hinoki(settings=settings)
    built.include(echo, room, typed, private, speak, small, boom, browser, room_stats)
    return built


async def serve(cors: tuple[str, ...] = (), **execution: Any) -> tuple[KisoWorker, int]:
    worker = KisoWorker(app(cors, **execution), "127.0.0.1", 0)
    return worker, await worker.start()


async def test_text_and_binary_messages_are_echoed() -> None:
    worker, port = await serve()
    try:
        client = await Client.open(port, "/echo")
        assert client.status == 101
        client.send(TEXT, "olá".encode())
        assert await client.message() == (TEXT, "olá".encode())
        client.send(BINARY, b"\x00\x01\x02")
        assert await client.message() == (BINARY, b"\x00\x01\x02")
        client.send(PING, b"p")
        assert await client.receive() == (PONG, b"p")
        client.send(CLOSE, struct.pack(">H", 1000))
        assert await client.closed_with() == 1000
        client.close()
    finally:
        await worker.stop()


async def test_a_fragmented_message_arrives_whole() -> None:
    worker, port = await serve()
    try:
        client = await Client.open(port, "/echo")
        client.send(TEXT, b"hel", fin=False)
        client.send(0, b"lo", fin=True)
        assert await client.message() == (TEXT, b"hello")
        client.close()
    finally:
        await worker.stop()


async def test_path_and_query_arguments_are_bound() -> None:
    worker, port = await serve()
    try:
        client = await Client.open(port, "/rooms/lobby?size=3")
        assert await client.message() == (TEXT, b"lobby:3")
        client.close()
        bad = await Client.open(port, "/rooms/lobby?size=many")
        assert bad.status == 400
        bad.close()
    finally:
        await worker.stop()


async def test_typed_messages_are_validated_both_ways() -> None:
    worker, port = await serve()
    try:
        client = await Client.open(port, "/typed")
        client.send(TEXT, b'{"x": 1, "y": 2}')
        opcode, payload = await client.message()
        assert (opcode, core.decode_json(payload)) == (
            TEXT,
            {"x": 2, "y": 2, "by": "server"},
        )
        client.send(TEXT, b'{"x": "no"}')
        assert await client.closed_with() == 1008
        client.close()
        broken = await Client.open(port, "/typed")
        broken.send(TEXT, b"{not json")
        assert await broken.closed_with() == 1007
        broken.close()
    finally:
        await worker.stop()


async def test_the_handshake_is_refused_before_the_upgrade() -> None:
    worker, port = await serve()
    try:
        plain = await Client.open(port, "/echo", upgrade=False)
        assert plain.status == 426
        plain.close()
        badkey = await Client.open(port, "/echo", key="short")
        assert badkey.status == 400
        badkey.close()
        anonymous = await Client.open(port, "/private")
        assert anonymous.status == 401
        anonymous.close()
        weak = await Client.open(
            port, "/private", headers={"authorization": f"Bearer {token('other')}"}
        )
        assert weak.status == 403
        weak.close()
        allowed = await Client.open(
            port, "/private", headers={"authorization": f"Bearer {token('chat:read')}"}
        )
        assert allowed.status == 101
        assert await allowed.message() == (TEXT, b"u1")
        allowed.close()
    finally:
        await worker.stop()


async def test_origin_is_checked_against_the_route_and_the_host() -> None:
    worker, port = await serve()
    try:
        foreign = await Client.open(
            port, "/echo", headers={"origin": "https://evil.example"}
        )
        assert foreign.status == 403
        foreign.close()
        listed = await Client.open(
            port, "/browser", headers={"origin": "https://app.example"}
        )
        assert listed.status == 101
        listed.close()
        same = await Client.open(
            port, "/echo", headers={"origin": f"http://127.0.0.1:{port}"}
        )
        assert same.status == 101
        same.close()
    finally:
        await worker.stop()


async def test_a_subprotocol_is_negotiated_and_has_its_own_access() -> None:
    worker, port = await serve()
    try:
        offered = await Client.open(
            port, "/speak", headers={"sec-websocket-protocol": "unknown, open"}
        )
        assert offered.headers["sec-websocket-protocol"] == "open"
        assert await offered.message() == (TEXT, b"open")
        offered.close()
        none = await Client.open(port, "/speak")
        assert none.status == 400
        none.close()
        secure = await Client.open(
            port, "/speak", headers={"sec-websocket-protocol": "secure"}
        )
        assert secure.status == 401
        secure.close()
    finally:
        await worker.stop()


async def test_a_message_over_the_limit_closes_with_1009() -> None:
    worker, port = await serve()
    try:
        client = await Client.open(port, "/small")
        client.send(TEXT, b"x" * 16)
        assert await client.message() == (TEXT, b"16")
        client.send(TEXT, b"x" * 17)
        assert await client.closed_with() == 1009
        client.close()
    finally:
        await worker.stop()


async def test_malformed_frames_close_with_a_protocol_error() -> None:
    worker, port = await serve()
    try:
        client = await Client.open(port, "/echo")
        client.writer.write(b"\x81\x01x")  # an unmasked client frame
        assert await client.closed_with() == 1002
        client.close()
    finally:
        await worker.stop()


async def test_a_failing_handler_closes_with_1011() -> None:
    worker, port = await serve()
    try:
        client = await Client.open(port, "/boom")
        assert await client.closed_with() == 1011
        client.close()
    finally:
        await worker.stop()


async def test_connections_over_the_limit_are_refused_with_503() -> None:
    worker, port = await serve(max_websockets=1)
    try:
        first = await Client.open(port, "/echo")
        assert first.status == 101
        second = await Client.open(port, "/echo")
        assert second.status == 503
        second.close()
        first.send(CLOSE, struct.pack(">H", 1000))
        assert await first.closed_with() == 1000
        first.close()
        await asyncio.sleep(0.2)
        third = await Client.open(port, "/echo")
        assert third.status == 101
        third.close()
    finally:
        await worker.stop()


async def test_shutdown_closes_open_connections_with_1001() -> None:
    worker, port = await serve()
    client = await Client.open(port, "/echo")
    assert client.status == 101
    stopping = asyncio.create_task(worker.stop())
    assert await client.closed_with() == 1001
    client.close()
    await stopping


def test_the_openapi_document_describes_websocket_actions() -> None:
    document = app().openapi()
    described = document["x-hinoki-websockets"]
    assert described["/typed"]["message"] == {"$ref": "#/components/schemas/Move"}
    assert described["/typed"]["reply"] == {"$ref": "#/components/schemas/Moved"}
    assert described["/speak"]["subprotocols"] == ["open", "secure"]
    assert described["/private"]["handshake"]["responses"]["401"]
    assert "/echo" not in document["paths"]


async def test_a_cors_wildcard_does_not_open_websockets_to_any_site() -> None:
    worker, port = await serve(cors=("*",))
    try:
        foreign = await Client.open(
            port, "/echo", headers={"origin": "https://evil.example"}
        )
        assert foreign.status == 403
        foreign.close()
    finally:
        await worker.stop()
    worker, port = await serve(cors=("https://app.example",))
    try:
        listed = await Client.open(
            port, "/echo", headers={"origin": "https://app.example"}
        )
        assert listed.status == 101
        listed.close()
    finally:
        await worker.stop()


async def test_a_plain_get_reaches_the_http_route_beside_a_websocket_pattern() -> None:
    worker, port = await serve()
    try:
        plain = await Client.open(port, "/rooms/stats", upgrade=False)
        assert plain.status == 200
        assert core.decode_json(plain.body) == {"stats": "yes"}
        plain.close()
        upgraded = await Client.open(port, "/rooms/stats")
        assert upgraded.status == 101
        upgraded.close()
        bare = await Client.open(port, "/rooms/lobby", upgrade=False)
        assert bare.status == 426
        bare.close()
    finally:
        await worker.stop()


async def test_an_unsupported_version_is_426() -> None:
    worker, port = await serve()
    try:
        old = await Client.open(
            port,
            "/echo",
            upgrade=False,
            headers={
                "connection": "upgrade",
                "upgrade": "websocket",
                "sec-websocket-version": "8",
                "sec-websocket-key": base64.b64encode(os.urandom(16)).decode(),
            },
        )
        assert old.status == 426
        old.close()
    finally:
        await worker.stop()


def test_the_settings_reject_bad_websocket_limits() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        ExecutionSettings(max_websockets=0)
    with pytest.raises(ValueError, match="at least 1"):
        ExecutionSettings(ws_queue=0)


class FakeTransport:
    """A scripted peer: ``inbox`` is what it receives, ``accepts`` what it sends."""

    def __init__(
        self, inbox: list[tuple[str, str | bytes]], accepts: bool = True
    ) -> None:
        self.inbox = inbox
        self.accepts = accepts
        self.closed: tuple[int, str] | None = None
        self.sent: list[str | bytes] = []

    async def receive(self) -> tuple[str, str | bytes] | None:
        return self.inbox.pop(0) if self.inbox else None

    async def send_text(self, text: str) -> bool:
        self.sent.append(text)
        return self.accepts

    async def send_bytes(self, data: bytes) -> bool:
        self.sent.append(data)
        return self.accepts

    async def close(self, code: int = 1000, reason: str = "") -> bool:
        self.closed = (code, reason)
        return True


async def test_receive_bytes_returns_a_binary_message() -> None:
    socket = http.WebSocket(FakeTransport([("bytes", b"\x00\x01")]), path="/x")
    assert await socket.receive_bytes() == b"\x00\x01"


@pytest.mark.parametrize(
    ("kind", "method", "reason"),
    [
        ("bytes", "receive_text", "text expected"),
        ("text", "receive_bytes", "binary expected"),
    ],
)
async def test_the_wrong_kind_of_message_closes_with_1003(
    kind: str, method: str, reason: str
) -> None:
    transport = FakeTransport([(kind, b"x" if kind == "bytes" else "x")])
    socket = http.WebSocket(transport, path="/x")
    with pytest.raises(WebSocketClosed) as caught:
        await getattr(socket, method)()
    assert (caught.value.code, caught.value.reason) == (1003, reason)
    assert socket.closed == (1003, reason)


async def test_receive_json_validates_with_the_given_schema() -> None:
    transport = FakeTransport([("text", '{"x": 1, "y": 2}'), ("text", '{"x": 1}')])
    socket = http.WebSocket(transport, path="/x")
    assert await socket.receive_json(Move) == Move(x=1, y=2)
    with pytest.raises(WebSocketClosed) as caught:
        await socket.receive_json(Move)
    assert caught.value.code == 1008


async def test_receive_json_without_a_schema_returns_the_parsed_value() -> None:
    socket = http.WebSocket(FakeTransport([("text", "[1, 2]")]), path="/x")
    assert await socket.receive_json() == [1, 2]


async def test_sending_to_a_lost_peer_raises_websocket_closed() -> None:
    socket = http.WebSocket(FakeTransport([], accepts=False), path="/x")
    with pytest.raises(WebSocketClosed) as lost:
        await socket.send_text("a")
    assert lost.value.code == 1006
    with pytest.raises(WebSocketClosed):
        await socket.send_bytes(b"a")


async def test_sending_after_a_close_reports_its_code() -> None:
    transport = FakeTransport([], accepts=False)
    transport.closed = (1001, "bye")
    socket = http.WebSocket(transport, path="/x")
    with pytest.raises(WebSocketClosed) as gone:
        await socket.send_text("a")
    assert (gone.value.code, gone.value.reason) == (1001, "bye")


async def test_a_typed_stream_refuses_a_binary_message() -> None:
    transport = FakeTransport([("bytes", b"x")])
    socket = http.WebSocket(transport, path="/x", message=Move)
    assert [item async for item in socket] == []
    assert transport.closed == (1003, "text expected")


def test_a_path_value_converts_to_a_bool_or_is_refused() -> None:
    flag = Param("on", bool, True, True, None)
    assert [convert(flag, text) for text in ("true", "1", "FALSE", "0")] == [
        True,
        True,
        False,
        False,
    ]
    with pytest.raises(ValueError, match="maybe"):
        convert(flag, "maybe")


@pytest.mark.parametrize("text", ["nan", "inf", "-Infinity"])
def test_a_non_finite_float_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match=text):
        convert(Param("ratio", float, True, True, None), text)


def test_a_websocket_on_a_staticmethod_is_refused() -> None:
    class Bad(API, path="/bad"):
        @staticmethod
        @http.websocket("/x")
        async def handler(socket: http.WebSocket) -> None:  # noqa: ARG004
            return None

    with pytest.raises(RegistrationError, match="plain methods"):
        Hinoki(include=[Bad])


def test_an_api_class_with_websockets_must_be_created_without_arguments() -> None:
    class Needy(API, path="/needy"):
        def __init__(self, token: str) -> None:
            self.token = token

        @http.websocket("/x")
        async def handler(self, socket: http.WebSocket) -> None:
            return None

    with pytest.raises(RegistrationError, match="without arguments"):
        Hinoki(include=[Needy])


def test_an_unresolvable_annotation_is_a_registration_error() -> None:
    async def handler(socket: http.WebSocket, size: Missing) -> None:  # type: ignore[name-defined]  # noqa: F821
        return None

    declared = http.websocket("/x")(handler)  # read when included
    with pytest.raises(RegistrationError, match="cannot resolve annotations"):
        Hinoki(include=[declared])


def test_a_repeated_or_missing_query_parameter_is_refused_before_the_upgrade() -> None:
    size = Param("size", int, path=False, required=True)
    with pytest.raises(HTTPError, match="invalid_parameter") as twice:
        value_of(size, {}, {"size": ["1", "2"]})
    assert twice.value.response.status == 400
    with pytest.raises(HTTPError, match="invalid_parameter") as missing:
        value_of(size, {}, {})
    assert b"is required" in missing.value.response.body
    assert value_of(size, {}, {"size": ["3"]}) == 3


async def test_a_cancelled_handler_propagates_without_closing_the_connection() -> None:
    async def handler(socket: http.WebSocket) -> None:
        raise asyncio.CancelledError

    route = WebSocketRoute(
        "/x", handler, None, None, None, (), (), None, None, socket="socket"
    )
    closes: list[int] = []

    class FakeTransport:
        closed = None

        async def close(self, code: int = 1000, reason: str = "") -> bool:
            closes.append(code)
            return True

    with pytest.raises(asyncio.CancelledError):
        await run(route, FakeTransport(), Admitted({}, None), "/x")  # type: ignore[arg-type]
    assert closes == []
