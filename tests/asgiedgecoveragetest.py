"""ASGI boundary behavior for unsupported scopes and shutdown failure."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from hinoki import Hinoki, http
from hinoki.errors import ShutdownError, StartupError
from hinoki.runtime.asgi import PATHSEND, ASGIAdapter
from hinoki.runtime.asgisocket import ASGITransport, connected
from tests.harness import Client, Feed, Sink


@pytest.mark.asyncio
async def test_adapter_rejects_unknown_scope_type() -> None:
    adapter = cast(Any, object.__new__(ASGIAdapter))
    with pytest.raises(RuntimeError, match="unsupported scope type 'webtransport'"):
        await adapter({"type": "webtransport"}, None, None)


@pytest.mark.asyncio
async def test_adapter_reports_lifespan_shutdown_failure() -> None:
    adapter = cast(Any, object.__new__(ASGIAdapter))

    async def shutdown() -> None:
        raise ShutdownError([RuntimeError("cleanup failed")])

    adapter.lifespan = type("Lifespan", (), {"shutdown": staticmethod(shutdown)})()
    sent: list[dict[str, str]] = []

    async def send(message: dict[str, str]) -> None:
        sent.append(message)

    await adapter._shutdown(send)
    assert sent == [
        {
            "type": "lifespan.shutdown.failed",
            "message": "shutdown finished with errors: RuntimeError",
        }
    ]


@pytest.mark.asyncio
async def test_adapter_applies_logging_and_reports_startup_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = cast(Any, object.__new__(ASGIAdapter))
    applied: list[bool] = []

    async def startup() -> None:
        raise StartupError("database", ValueError("unavailable"))

    adapter.pipeline = SimpleNamespace(
        logging=SimpleNamespace(apply=lambda: applied.append(True)),
        debug=True,
        hosts=None,
        app=SimpleNamespace(settings=None),
    )
    adapter.lifespan = SimpleNamespace(startup=startup)
    sent: list[dict[str, str]] = []

    async def send(message: dict[str, str]) -> None:
        sent.append(message)

    assert await adapter._startup(send) is True
    assert applied == [True]
    assert sent == [
        {
            "type": "lifespan.startup.failed",
            "message": "module 'database' failed to start: ValueError",
        }
    ]
    assert "debug is on" in caplog.text
    assert "startup failed" in caplog.text


async def test_a_file_is_handed_to_a_server_that_offers_pathsend(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.txt").write_text("hello")

    @http.get("/a")
    async def a() -> http.File:
        return http.File("a.txt", root=tmp_path)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/a",
        "query_string": b"",
        "headers": [],
        "extensions": {PATHSEND: {}},
    }
    sink = Sink()
    adapter = ASGIAdapter(Hinoki(include=[a]))
    lifespan = Client(adapter)
    assert await lifespan.lifespan_event("startup") == "lifespan.startup.complete"
    await adapter(
        scope, Feed({"type": "http.request", "body": b"", "more_body": False}), sink
    )
    assert await lifespan.lifespan_event("shutdown") == "lifespan.shutdown.complete"
    assert [m["type"] for m in sink.sent] == ["http.response.start", PATHSEND]
    assert sink.sent[1]["path"] == str((tmp_path / "a.txt").resolve())


async def test_the_transport_reports_a_disconnect_once_and_stays_closed() -> None:
    sink = Sink()
    events = [{"type": "websocket.disconnect", "code": 1001}]

    async def receive() -> dict[str, Any]:
        return events.pop()

    transport = ASGITransport(receive, sink, 10)
    assert await transport.receive() is None
    assert transport.closed == (1001, "")
    assert await transport.receive() is None  # no second read from the server
    assert not await transport.send_bytes(b"late")
    assert sink.sent == []


async def test_a_send_the_server_refuses_closes_the_transport() -> None:
    async def receive() -> dict[str, Any]:
        raise AssertionError("not read")

    async def send(event: dict[str, Any]) -> None:
        raise ConnectionError("peer gone")

    transport = ASGITransport(receive, send, 10)
    assert not await transport.send_bytes(b"x")
    assert transport.closed == (1001, "")


async def test_a_first_event_that_is_not_a_connect_ends_the_handshake() -> None:
    assert not await connected(Feed({"type": "websocket.disconnect"}))
    assert await connected(Feed({"type": "websocket.connect"}))
