"""Debug with ``allowed_hosts = *``: an app that serves the admin or WebSocket
routes accepts only loopback Host names, so DNS rebinding cannot reach it."""

from __future__ import annotations

import asyncio
import io
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from hinoki import Hinoki, http
from hinoki.admin import Admin
from hinoki.db import DatabaseModule
from hinoki.runtime.kiso import KisoWorker
from hinoki.settings import AppSettings
from hinoki.testing import ASGIClient
from hinoki.wsgi import WSGIAdapter
from tests.harness import Client
from tests.websockettest import Client as SocketClient

SECRET = "s" * 40
_WORK = tempfile.TemporaryDirectory(prefix="hinoki-loopback-")
DATABASE = f"sqlite://{Path(_WORK.name) / 'app.sqlite3'}"
LOOPBACK = [
    "localhost",
    "localhost:8000",
    "LOCALHOST.",
    "app.localhost:8000",
    "127.0.0.1",
    "127.0.0.1:8000",
    "127.9.8.7",
    "[::1]:8000",
    "[::1]",
]
OTHERS = [
    "evil.example",
    "evil.example:8000",
    "192.168.1.5",
    "10.0.0.1:8000",
    "localhost.evil.example",
    "127.0.0.1.evil.example",
    "128.0.0.1",
    "[::2]:8000",
    "",
]


@http.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@http.websocket("/live")
async def live(socket: http.WebSocket) -> None:
    await socket.send_text("hi")


def admin_app(**settings: Any) -> Hinoki:
    return Hinoki(
        settings=AppSettings(**settings),
        include=[health, Admin(secret=SECRET)],
        modules=[DatabaseModule(DATABASE, max_size=1)],
    )


def socket_app(**settings: Any) -> Hinoki:
    return Hinoki(settings=AppSettings(**settings), include=[health, live])


def plain_app(**settings: Any) -> Hinoki:
    return Hinoki(settings=AppSettings(**settings), include=[health])


async def status_of(
    app: Hinoki,
    host: str | None,
    *,
    method: str = "GET",
    path: str = "/health",
    extra: dict[str, str] | None = None,
) -> tuple[int, Any]:
    headers = {} if host is None else {"host": host}
    client = Client(app.asgi())
    await started(client)
    try:
        reply = await client.request(method, path, headers={**headers, **(extra or {})})
    finally:
        await client.lifespan_event("shutdown")
    return reply.status, reply.body


async def started(client: Client) -> None:
    """A served lifespan, as a server sends it (no in-process key)."""
    assert await client.lifespan_event("startup") == "lifespan.startup.complete"


async def handshake(app: Hinoki, host: str) -> bool:
    """Drive the ASGI adapter with a WebSocket scope; True when accepted."""
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await inbox.put({"type": "websocket.connect"})
    sent: list[str] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message["type"])
        if message["type"] == "websocket.accept":
            await inbox.put({"type": "websocket.disconnect", "code": 1000})

    scope: Any = {
        "type": "websocket",
        "scheme": "ws",
        "path": "/live",
        "raw_path": b"/live",
        "query_string": b"",
        "headers": [(b"host", host.encode())],
        "client": ("127.0.0.1", 5000),
        "server": ("127.0.0.1", 8000),
        "asgi": {"version": "3.0"},
        "subprotocols": [],
    }
    client = Client(app.asgi())
    await started(client)
    try:
        await asyncio.wait_for(client.adapter(scope, inbox.get, send), 5)
    finally:
        await client.lifespan_event("shutdown")
    return "websocket.accept" in sent


@pytest.mark.parametrize("make", [admin_app, socket_app])
@pytest.mark.parametrize("host", LOOPBACK)
async def test_debug_with_any_host_accepts_loopback_hosts(make: Any, host: str) -> None:
    assert (await status_of(make(debug=True), host))[0] == 200


@pytest.mark.parametrize("make", [admin_app, socket_app])
@pytest.mark.parametrize("host", OTHERS)
async def test_debug_with_any_host_refuses_other_hosts_even_for_get(
    make: Any, host: str
) -> None:
    status, body = await status_of(make(debug=True), host)
    assert status == 400
    assert "disallowed_host" in str(body)


@pytest.mark.parametrize("host", LOOPBACK)
async def test_websockets_accept_loopback_hosts_in_debug(host: str) -> None:
    assert await handshake(socket_app(debug=True), host) is True


@pytest.mark.parametrize("host", OTHERS)
async def test_websockets_refuse_other_hosts_in_debug(host: str) -> None:
    assert await handshake(socket_app(debug=True), host) is False


@pytest.mark.parametrize("host", [*LOOPBACK, *OTHERS, None])
async def test_a_plain_http_app_still_accepts_any_host_in_debug(
    host: str | None,
) -> None:
    assert (await status_of(plain_app(debug=True), host))[0] == 200


async def test_explicit_allowed_hosts_win_over_the_loopback_rule() -> None:
    for make in (admin_app, socket_app):
        app = make(debug=True, allowed_hosts=("app.example",))
        assert (await status_of(app, "app.example"))[0] == 200
        assert (await status_of(app, "localhost"))[0] == 400
        assert (await status_of(app, "evil.example"))[0] == 400
    assert await handshake(socket_app(allowed_hosts=("app.example",)), "app.example")
    assert not await handshake(
        socket_app(debug=True, allowed_hosts=("app.example",)), "localhost"
    )


async def test_a_dns_rebinding_admin_post_is_refused_before_csrf() -> None:
    app = admin_app(debug=True)
    status, body = await status_of(
        app,
        "evil.example",
        method="POST",
        path="/admin/login",
        extra={
            "origin": "http://evil.example",
            "content-type": "application/x-www-form-urlencoded",
        },
    )
    assert status == 400
    assert "disallowed_host" in str(body)


async def test_the_in_process_clients_stay_exempt() -> None:
    async with ASGIClient(admin_app(debug=True)) as client:
        assert (await client.get("/health")).status == 200
    async with ASGIClient(socket_app(debug=True)) as client:
        assert (await client.get("/health")).status == 200


async def test_a_loopback_only_app_warns_at_a_served_startup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = Client(admin_app(debug=True).asgi())
    with caplog.at_level("WARNING", logger="hinoki.runtime"):
        await started(client)
    assert any("only loopback Host names" in r.getMessage() for r in caplog.records)
    await client.lifespan_event("shutdown")


def wsgi_status(app: Hinoki, host: str) -> str:
    adapter = WSGIAdapter(app)
    seen: list[str] = []
    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/health",
        "SERVER_NAME": "t",
        "SERVER_PORT": "80",
        "HTTP_HOST": host,
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(b""),
    }
    try:
        b"".join(adapter(environ, lambda status, _headers: seen.append(status)))
    finally:
        adapter.close()
    return seen[0]


def test_the_wsgi_adapter_applies_the_loopback_rule() -> None:
    app = admin_app(debug=True)
    assert wsgi_status(app, "localhost:8000").startswith("200")
    assert wsgi_status(app, "evil.example").startswith("400")


async def raw_get(port: int, host: str, path: str = "/health") -> int:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"GET {path} HTTP/1.1\r\nhost: {host}\r\nconnection: close\r\n\r\n".encode()
    )
    head = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    return int(head.split(b" ", 2)[1])


async def socket_status(port: int, host: str) -> int:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        (
            f"GET /live HTTP/1.1\r\nhost: {host}\r\nconnection: upgrade\r\n"
            "upgrade: websocket\r\nsec-websocket-version: 13\r\n"
            "sec-websocket-key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"
        ).encode()
    )
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    writer.close()
    return int(head.split(b" ", 2)[1])


@pytest.fixture
async def kiso_port() -> AsyncIterator[int]:
    app = Hinoki(
        settings=AppSettings(debug=True),
        include=[health, live, Admin(secret=SECRET)],
        modules=[DatabaseModule(DATABASE, max_size=1)],
    )
    worker = KisoWorker(app, "127.0.0.1", 0)
    port = await worker.start()
    try:
        yield port
    finally:
        await worker.stop()


async def test_kiso_applies_the_loopback_rule_on_both_lanes(kiso_port: int) -> None:
    # /health is a native route, /admin/login runs through Python.
    for path in ("/health", "/admin/login"):
        assert await raw_get(kiso_port, f"localhost:{kiso_port}", path) != 400
        assert await raw_get(kiso_port, f"127.0.0.1:{kiso_port}", path) != 400
        assert await raw_get(kiso_port, f"[::1]:{kiso_port}", path) != 400
        assert await raw_get(kiso_port, "evil.example", path) == 400
        assert await raw_get(kiso_port, "192.168.1.5", path) == 400


async def test_kiso_refuses_a_websocket_handshake_to_a_foreign_host(
    kiso_port: int,
) -> None:
    assert await socket_status(kiso_port, f"localhost:{kiso_port}") == 101
    assert await socket_status(kiso_port, "evil.example") == 400
    client = await SocketClient.open(kiso_port, "/live")
    assert client.status == 101
    client.close()
