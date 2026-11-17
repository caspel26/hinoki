"""ASGI boundary behavior for unsupported scopes and shutdown failure."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from hinoki.errors import ShutdownError, StartupError
from hinoki.runtime.asgi import ASGIAdapter


@pytest.mark.asyncio
async def test_adapter_rejects_non_http_non_lifespan_scope() -> None:
    adapter = cast(Any, object.__new__(ASGIAdapter))
    with pytest.raises(RuntimeError, match="unsupported scope type 'websocket'"):
        await adapter({"type": "websocket"}, None, None)


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
        logging=SimpleNamespace(apply=lambda: applied.append(True)), debug=True
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
