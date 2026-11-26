"""The WebSocket Origin rule, one table through both lanes.

The Python (ASGI) lane calls the Rust predicate and the Kiso lane runs it in the
server, so a row that differs between them is a defect. Kiso is exercised over
a real plain loopback handshake; the TLS rows (where the connection scheme
changes the answer) go through the ASGI lane, which reports ``wss``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from hinoki import Hinoki, http
from hinoki.runtime.asgisocket import origin_allowed
from hinoki.runtime.kiso import KisoWorker
from hinoki.settings import AppSettings, CorsSettings, ServerSettings
from hinoki.websocket import WebSocketRoute
from tests.websockettest import Client

LISTED = "https://app.example"
CORS_LISTED = "https://cors.example"
ROUTES = {"none": [], "listed": [LISTED], "star": ["*"]}

# (origin, route, cors origins, tls, accepted); {host} is the request Host.
Row = tuple[str | None, str, tuple[str, ...], bool, bool]
MATRIX: list[Row] = [
    (None, "none", (), False, True),
    ("http://{host}", "none", (), False, True),
    ("HTTP://{HOST}", "none", (), False, True),
    ("https://{host}", "none", (), False, False),  # scheme differs from ws
    ("http://{host}", "none", (), True, False),  # scheme differs from wss
    ("https://{host}", "none", (), True, True),
    ("http://{host}.", "none", (), False, False),  # no trailing-dot folding
    ("http://127.0.0.1", "none", (), False, False),  # port is part of the host
    ("null", "none", (), False, False),
    ("https://evil.example", "none", (), False, False),
    (LISTED, "none", (), False, False),
    (LISTED, "listed", (), False, True),
    (LISTED.upper(), "listed", (), False, True),
    ("https://evil.example", "listed", (), False, False),
    ("https://evil.example", "star", (), False, True),
    ("https://evil.example", "none", ("*",), False, False),  # CORS * not honoured
    ("*", "none", ("*",), False, False),
    (CORS_LISTED, "none", (CORS_LISTED,), False, True),
    (CORS_LISTED.upper(), "none", (CORS_LISTED,), False, True),
]


def fill(origin: str | None, host: str) -> str | None:
    if origin is None:
        return None
    return origin.replace("{host}", host).replace("{HOST}", host.upper())


@pytest.mark.parametrize(("origin", "route", "cors", "tls", "accepted"), MATRIX)
def test_the_asgi_lane_follows_the_table(
    origin: str | None, route: str, cors: tuple[str, ...], tls: bool, accepted: bool
) -> None:
    host = "app.test:8000"
    headers = [(b"host", host.encode())]
    value = fill(origin, host)
    if value is not None:
        headers.append((b"origin", value.encode()))
    scope: Any = {"scheme": "wss" if tls else "ws", "headers": headers}
    stub = cast(WebSocketRoute, SimpleNamespace(origins=ROUTES[route]))
    lowered = frozenset(item.lower() for item in cors)
    assert origin_allowed(stub, scope, lowered) is accepted


def test_a_missing_host_never_matches_the_same_origin_rule() -> None:
    scope: Any = {"scheme": "ws", "headers": [(b"origin", b"http://")]}
    stub = cast(WebSocketRoute, SimpleNamespace(origins=[]))
    assert origin_allowed(stub, scope) is False


@http.websocket("/none")
async def none(socket: http.WebSocket) -> None:
    await socket.send_text("hi")


@http.websocket("/listed", origins=[LISTED])
async def listed(socket: http.WebSocket) -> None:
    await socket.send_text("hi")


@http.websocket("/star", origins=["*"])
async def star(socket: http.WebSocket) -> None:
    await socket.send_text("hi")


async def test_the_kiso_lane_follows_the_same_table() -> None:
    for cors in {row[2] for row in MATRIX}:
        built = Hinoki(
            settings=AppSettings(
                allowed_hosts=("127.0.0.1",), cors=CorsSettings(origins=cors)
            )
        )
        built.include(none, listed, star)
        worker = KisoWorker(built, "127.0.0.1", 0)
        port = await worker.start()
        try:
            for origin, route, row_cors, tls, accepted in MATRIX:
                if tls or row_cors != cors:
                    continue
                value = fill(origin, f"127.0.0.1:{port}")
                headers = {} if value is None else {"origin": value}
                client = await Client.open(port, f"/{route}", headers=headers)
                try:
                    assert (client.status == 101) is accepted, (origin, route, cors)
                finally:
                    client.close()
        finally:
            await worker.stop()


PROXY_ROWS = [
    ("https", "https", True),
    ("http", "https", False),
    ("http", None, True),
    ("https", None, False),
]


async def asgi_handshake(app: Hinoki, origin: str, proto: str | None) -> bool:
    """Drive the whole ASGI adapter from a trusted peer; True when accepted."""
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await inbox.put({"type": "websocket.connect"})
    sent: list[str] = []

    async def receive() -> dict[str, Any]:
        return await inbox.get()

    async def send(message: dict[str, Any]) -> None:
        sent.append(message["type"])
        if message["type"] == "websocket.accept":
            await inbox.put({"type": "websocket.disconnect", "code": 1000})

    headers = [(b"host", b"app.example"), (b"origin", origin.encode())]
    if proto is not None:
        headers.append((b"x-forwarded-proto", proto.encode()))
    scope: Any = {
        "type": "websocket",
        "scheme": "ws",
        "path": "/none",
        "raw_path": b"/none",
        "query_string": b"",
        "headers": headers,
        "client": ("10.1.2.3", 5000),
        "server": ("10.9.9.9", 8000),
        "asgi": {"version": "3.0"},
        "subprotocols": [],
    }
    await asyncio.wait_for(app.asgi()(scope, receive, send), 5)
    return "websocket.accept" in sent


@pytest.mark.parametrize(("origin_scheme", "proto", "accepted"), PROXY_ROWS)
async def test_a_trusted_proxy_reporting_https_counts_on_the_asgi_lane(
    origin_scheme: str, proto: str | None, accepted: bool
) -> None:
    settings = AppSettings(
        allowed_hosts=("app.example",),
        server=ServerSettings(trusted_proxies=("10.0.0.0/8",)),
    )
    app = Hinoki(include=[none], settings=settings)
    origin = f"{origin_scheme}://app.example"
    assert await asgi_handshake(app, origin, proto) is accepted


@pytest.mark.parametrize(("origin_scheme", "proto", "accepted"), PROXY_ROWS)
async def test_a_trusted_proxy_reporting_https_counts_on_the_kiso_lane(
    origin_scheme: str, proto: str | None, accepted: bool
) -> None:
    settings = AppSettings(
        allowed_hosts=("127.0.0.1",),
        server=ServerSettings(trusted_proxies=("127.0.0.1/32",)),
    )
    worker = KisoWorker(Hinoki(include=[none], settings=settings), "127.0.0.1", 0)
    port = await worker.start()
    try:
        headers = {"origin": f"{origin_scheme}://127.0.0.1:{port}"}
        if proto is not None:
            headers["x-forwarded-proto"] = proto
        client = await Client.open(port, "/none", headers=headers)
        try:
            assert (client.status == 101) is accepted
        finally:
            client.close()
    finally:
        await worker.stop()
