"""Scenario request validation and lifespan cleanup behavior."""

from __future__ import annotations

import asyncio
from typing import Any, NoReturn, cast

import pytest

from hinoki import Hinoki
from hinoki.scenario import (
    ScenarioError,
    build_scope,
    check_request,
    drive,
    drive_with_timeout,
    next_message,
)


@pytest.mark.parametrize("path", ["//other-host", "/line\nbreak", "/space here"])
def test_scenario_rejects_non_plain_paths(path: str) -> None:
    with pytest.raises(ScenarioError, match="plain URL path"):
        check_request("GET", path, "read")


def test_scenario_rejects_non_string_headers_and_forbidden_names() -> None:
    with pytest.raises(ScenarioError, match="headers is an object of strings"):
        build_scope("GET", "/", [("x-test", "value")], None, "read")
    with pytest.raises(ScenarioError, match="header 'Host' is not allowed"):
        build_scope("GET", "/", {"Host": "example.test"}, None, "read")


@pytest.mark.asyncio
async def test_next_lifespan_message_returns_none_when_task_exits_first() -> None:
    async def finished() -> None:
        return None

    task = asyncio.create_task(finished())
    await task
    outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    assert await next_message(outbox, task) is None
    assert outbox.empty()


@pytest.mark.asyncio
async def test_failed_startup_cancels_lifespan_task() -> None:
    finished = asyncio.Event()

    async def adapter(_scope: Any, receive: Any, send: Any) -> None:
        await receive()
        await send({"type": "lifespan.startup.failed", "message": "private detail"})
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    app = cast(Hinoki, type("ScenarioApp", (), {"asgi": lambda _self: adapter})())
    with pytest.raises(ScenarioError, match="application failed to start") as raised:
        await drive(app, {"type": "http", "method": "GET"}, b"")
    assert "private detail" not in str(raised.value)
    await asyncio.wait_for(finished.wait(), timeout=1)


@pytest.mark.asyncio
async def test_scenario_timeout_becomes_a_safe_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def wait_forever(_app: Any, _scope: Any, _body: bytes) -> NoReturn:
        await asyncio.Event().wait()
        raise AssertionError("event wait unexpectedly returned")

    monkeypatch.setattr("hinoki.scenario.drive", wait_forever)
    monkeypatch.setattr("hinoki.scenario.TIMEOUT", 0.001)
    with pytest.raises(ScenarioError, match=r"longer than 0\.001 seconds"):
        await drive_with_timeout(cast(Any, object()), {}, b"")
