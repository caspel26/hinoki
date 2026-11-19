"""``core.run_native`` serves a coroutine of native waits without an event loop."""

import asyncio
import contextvars
import threading
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from hinoki import core

Outcome = tuple[Any, BaseException | None, str | None]


def start(
    coro: Awaitable[Any],
    *,
    ctx: contextvars.Context | None = None,
    deadline_ms: int | None = None,
    scheduler: Callable[..., Any] | None = None,
    on_foreign: Callable[[Any, Any, Any, float | None], None] | None = None,
) -> tuple[core._NativeRun, asyncio.Future[Outcome]]:
    """Start a drive; the future resolves with what ``on_done`` received."""
    loop = asyncio.get_running_loop()
    done: asyncio.Future[Outcome] = loop.create_future()

    def on_done(value: Any, error: BaseException | None, reason: str | None) -> None:
        loop.call_soon_threadsafe(done.set_result, (value, error, reason))

    def refuse(coro: Any, ctx: Any, yielded: Any, remaining: float | None) -> None:
        raise AssertionError("unexpected hand-over")

    run = core.run_native(
        coro, on_done, on_foreign or refuse, ctx, deadline_ms, scheduler
    )
    return run, done


async def test_a_coroutine_without_waits_needs_no_other_thread() -> None:
    async def handler() -> str:
        return "now"

    _, done = start(handler())
    assert await done == ("now", None, None)


async def test_waits_run_without_the_event_loop_and_resume_the_coroutine() -> None:
    async def handler() -> list[Any]:
        first = await core._native_wait(5, "a", False)
        second = await core._native_wait(5, "b", False)
        return [first, second]

    _, done = start(handler())
    assert await done == (["a", "b"], None, None)


async def test_a_failed_wait_raises_inside_the_coroutine() -> None:
    async def handler() -> str:
        try:
            await core._native_wait(1, "x", True)
        except RuntimeError as error:
            return f"caught {error}"
        return "missed"

    _, done = start(handler())
    assert await done == ("caught native wait failed", None, None)


async def test_an_uncaught_error_reaches_on_done() -> None:
    async def handler() -> None:
        await core._native_wait(1, "x", False)
        raise ValueError("boom")

    _, done = start(handler())
    value, error, reason = await done
    assert value is None
    assert isinstance(error, ValueError)
    assert reason is None


async def test_the_deadline_cancels_at_the_wait_and_cleanup_still_runs() -> None:
    steps: list[str] = []

    async def handler() -> None:
        try:
            await core._native_wait(10_000, "slow", False)
        finally:
            steps.append("finally")
            await core._native_wait(20, "rollback", False)
            steps.append("cleaned")

    _, done = start(handler(), deadline_ms=30)
    _, error, reason = await asyncio.wait_for(done, 2)
    assert isinstance(error, asyncio.CancelledError)
    assert reason == "deadline"
    assert steps == ["finally", "cleaned"]


async def test_a_handler_that_swallows_the_deadline_still_answers() -> None:
    async def handler() -> str:
        try:
            await core._native_wait(10_000, "slow", False)
        except asyncio.CancelledError:
            return "recovered"
        return "unreachable"

    _, done = start(handler(), deadline_ms=20)
    assert await asyncio.wait_for(done, 2) == ("recovered", None, "deadline")


async def test_cancel_stops_the_coroutine_like_a_disconnect() -> None:
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    steps: list[str] = []

    async def handler() -> None:
        try:
            loop.call_soon_threadsafe(entered.set)
            await core._native_wait(10_000, "slow", False)
        finally:
            steps.append("finally")

    run, done = start(handler())
    await entered.wait()
    run.cancel()
    _, error, reason = await asyncio.wait_for(done, 2)
    assert isinstance(error, asyncio.CancelledError)
    assert reason == "cancelled"
    assert steps == ["finally"]


async def test_cancel_after_the_end_is_harmless() -> None:
    async def handler() -> int:
        return 1

    run, done = start(handler())
    run.cancel()
    assert await done == (1, None, None)


async def test_every_step_runs_in_the_given_context() -> None:
    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker")
    seen: list[str] = []

    async def handler() -> None:
        seen.append(marker.get())
        await core._native_wait(5, None, False)
        seen.append(marker.get())

    ctx = contextvars.copy_context()
    ctx.run(marker.set, "request")
    _, done = start(handler(), ctx=ctx)
    await done
    assert seen == ["request", "request"]
    assert marker.get(None) is None


async def test_a_foreign_await_hands_the_coroutine_over_with_the_time_left() -> None:
    loop = asyncio.get_running_loop()
    pending: asyncio.Future[str] = loop.create_future()
    handed: list[tuple[Any, float | None]] = []

    async def handler() -> str:
        return await pending

    def take_over(coro: Any, ctx: Any, yielded: Any, remaining: float | None) -> None:
        handed.append((yielded, remaining))
        coro.close()

    _, done = start(handler(), deadline_ms=5_000, on_foreign=take_over)
    assert len(handed) == 1
    yielded, remaining = handed[0]
    assert yielded is pending
    assert remaining is not None
    assert 0 < remaining <= 5
    assert not done.done()
    pending.cancel()


async def test_a_foreign_await_after_a_native_one_is_handed_over() -> None:
    loop = asyncio.get_running_loop()
    pending: asyncio.Future[str] = loop.create_future()
    handed: asyncio.Future[Any] = loop.create_future()

    async def handler() -> str:
        await core._native_wait(5, None, False)
        return await pending

    def take_over(coro: Any, ctx: Any, yielded: Any, remaining: float | None) -> None:
        loop.call_soon_threadsafe(handed.set_result, (coro, yielded, remaining))

    start(handler(), on_foreign=take_over)
    coro, yielded, remaining = await asyncio.wait_for(handed, 2)
    assert yielded is pending
    assert remaining is None
    coro.close()
    pending.cancel()


async def test_a_failing_hand_over_ends_the_drive_with_its_error() -> None:
    pending: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def handler() -> str:
        return await pending

    def take_over(coro: Any, ctx: Any, yielded: Any, remaining: float | None) -> None:
        raise LookupError("no taker")

    _, done = start(handler(), on_foreign=take_over)
    _, error, _ = await done
    assert isinstance(error, LookupError)
    pending.cancel()


def test_the_handle_is_cancelable_without_a_loop() -> None:
    async def handler() -> int:
        return 1

    outcomes: list[Outcome] = []
    run = core.run_native(
        handler(),
        lambda value, error, reason: outcomes.append((value, error, reason)),
        lambda *_: pytest.fail("no hand-over expected"),
    )
    run.cancel()
    assert outcomes == [(1, None, None)]


async def test_with_a_scheduler_every_step_runs_on_the_loop_thread() -> None:
    loop = asyncio.get_running_loop()
    threads: list[int] = []
    loops: list[object] = []

    async def handler() -> str:
        for _ in range(3):
            await core._native_wait(2, None, False)
            threads.append(threading.get_ident())
            loops.append(asyncio.get_running_loop())
        return "done"

    _, done = start(handler(), scheduler=loop.call_soon_threadsafe)
    assert await asyncio.wait_for(done, 2) == ("done", None, None)
    assert set(threads) == {threading.get_ident()}
    assert all(seen is loop for seen in loops)


async def test_without_a_scheduler_there_is_no_loop_off_the_first_step() -> None:
    errors: list[type[BaseException]] = []

    async def handler() -> None:
        await core._native_wait(2, None, False)
        try:
            asyncio.get_running_loop()
        except RuntimeError as error:
            errors.append(type(error))

    _, done = start(handler())
    await done
    assert errors == [RuntimeError]


async def test_the_deadline_still_cancels_through_the_scheduler() -> None:
    loop = asyncio.get_running_loop()
    steps: list[str] = []

    async def handler() -> None:
        try:
            await core._native_wait(10_000, None, False)
        finally:
            await core._native_wait(5, None, False)
            steps.append("cleaned")

    _, done = start(handler(), deadline_ms=20, scheduler=loop.call_soon_threadsafe)
    _, error, reason = await asyncio.wait_for(done, 2)
    assert isinstance(error, asyncio.CancelledError)
    assert reason == "deadline"
    assert steps == ["cleaned"]


async def test_a_scheduler_that_fails_ends_the_drive_with_its_error() -> None:
    def broken(callback: Any) -> None:
        raise RuntimeError("loop is closed")

    async def handler() -> None:
        await core._native_wait(2, None, False)

    _, done = start(handler(), scheduler=broken)
    _, error, _ = await asyncio.wait_for(done, 2)
    assert isinstance(error, RuntimeError)
    assert "closed" in str(error)
