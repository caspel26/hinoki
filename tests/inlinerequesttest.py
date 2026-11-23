"""A request that never suspends runs without a timer; one that suspends keeps its
full deadline, cancellation and error translation."""

import asyncio
import time
from collections.abc import Callable
from typing import Any

import pytest

from hinoki import Hinoki, RuntimeConfig, http
from hinoki.runtime.inline import RequestDeadline, continue_coroutine, run_inline
from tests.harness import Client, Pool

cleaned: list[str] = []


@http.get("/inline")
async def inline() -> dict[str, str]:
    return {"ok": "yes"}


@http.get("/yields")
async def yields() -> dict[str, str]:
    await asyncio.sleep(0)
    return {"ok": "yes"}


@http.get("/busy-then-wait")
async def busy_then_wait() -> dict[str, str]:
    time.sleep(0.15)
    await asyncio.sleep(5)
    return {"ok": "late"}


@http.get("/fails-after-wait")
async def fails_after_wait() -> dict[str, str]:
    await asyncio.sleep(0)
    raise RuntimeError("secret")


@http.get("/cleanup")
async def cleanup() -> dict[str, str]:
    try:
        await asyncio.sleep(5)
    finally:
        cleaned.append("done")
    return {"ok": "no"}


async def serve(pool: Pool, timeout: float = 30.0) -> Client:
    app = Hinoki(runtime=RuntimeConfig(request_timeout=timeout))
    app.include(inline, yields, busy_then_wait, fails_after_wait, cleanup)
    return await pool.start(app)


@pytest.fixture
def timers(monkeypatch: pytest.MonkeyPatch) -> list[float | None]:
    started: list[float | None] = []
    original: Callable[[float | None], Any] = asyncio.timeout

    def counting(delay: float | None) -> Any:
        started.append(delay)
        return original(delay)

    monkeypatch.setattr(asyncio, "timeout", counting)
    return started


async def test_a_request_that_never_suspends_starts_no_timer(
    pool: Pool, timers: list[float | None]
) -> None:
    client = await serve(pool)
    timers.clear()  # startup runs under its own timeout
    reply = await client.request("GET", "/inline")
    assert (reply.status, reply.body) == (200, {"ok": "yes"})
    assert timers == []


async def test_a_request_that_suspends_runs_under_the_timeout(
    pool: Pool, timers: list[float | None]
) -> None:
    client = await serve(pool)
    timers.clear()
    reply = await client.request("GET", "/yields")
    assert (reply.status, reply.body) == (200, {"ok": "yes"})
    assert len(timers) == 1


async def test_the_deadline_counts_from_the_start_of_the_request(pool: Pool) -> None:
    client = await serve(pool, timeout=0.1)
    started = time.monotonic()
    reply = await client.request("GET", "/busy-then-wait")
    assert reply.status == 504
    assert time.monotonic() - started < 1


async def test_an_error_after_a_suspension_is_still_translated(pool: Pool) -> None:
    client = await serve(pool)
    reply = await client.request("GET", "/fails-after-wait")
    assert reply.status == 500
    assert "secret" not in str(reply.body)


async def test_cancelling_a_suspended_request_unwinds_the_handler(pool: Pool) -> None:
    cleaned.clear()
    client = await serve(pool)
    task = asyncio.create_task(client.request("GET", "/cleanup"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned == ["done"]


async def test_the_deadline_cleans_up_a_suspended_handler(pool: Pool) -> None:
    cleaned.clear()
    client = await serve(pool, timeout=0.1)
    assert (await client.request("GET", "/cleanup")).status == 504
    assert cleaned == ["done"]


async def test_closing_the_inline_wait_closes_the_coroutine() -> None:
    closed: list[str] = []

    async def work() -> None:
        try:
            await asyncio.sleep(5)
        finally:
            closed.append("done")

    coro = work()
    yielded = coro.send(None)
    waiting = continue_coroutine(coro, yielded)
    assert waiting.send(None) is yielded
    waiting.close()
    assert closed == ["done"]


async def test_a_timeout_raised_by_the_handler_is_not_a_deadline() -> None:
    async def work() -> None:
        await asyncio.sleep(0)
        raise TimeoutError("upstream")

    with pytest.raises(TimeoutError, match="upstream") as raised:
        await run_inline(work(), 30.0)
    assert not isinstance(raised.value, RequestDeadline)
