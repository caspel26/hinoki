import asyncio
import contextvars
import inspect
import threading
import time
from collections.abc import Coroutine
from concurrent.futures import Future
from typing import Any

import pytest

from hinoki import blocking, context
from hinoki.runtime.executor import AbandonedResult, ExecutorSaturated, SyncExecutor

var: contextvars.ContextVar[str] = contextvars.ContextVar("var", default="unset")


def read_context() -> tuple[str, str]:
    return var.get(), threading.current_thread().name


async def coroutine_result() -> int:
    return 1


def delayed_coroutine_call(
    started: threading.Event,
    release: threading.Event,
    results: list[Coroutine[Any, Any, int]],
) -> Coroutine[Any, Any, int]:
    started.set()
    release.wait(5)
    coroutine = coroutine_result()
    results.append(coroutine)
    return coroutine


async def test_runs_on_worker_thread_with_copied_context() -> None:
    async with SyncExecutor(1, 0) as executor:
        var.set("request-1")
        value, thread = await executor.run(read_context)
    assert value == "request-1"
    assert thread.startswith("hinoki-sync")


async def test_does_not_block_the_event_loop() -> None:
    async with SyncExecutor(2, 0) as executor:
        task = asyncio.create_task(executor.run(time.sleep, 0.5))
        await asyncio.sleep(0.05)
        start = time.monotonic()
        await asyncio.sleep(0)
        assert time.monotonic() - start < 0.1
        await task


async def test_admission_is_bounded_by_workers_plus_queue() -> None:
    release = threading.Event()
    async with SyncExecutor(workers=1, queue=1) as executor:
        first = asyncio.create_task(executor.run(release.wait, 5))
        second = asyncio.create_task(executor.run(release.wait, 5))
        await asyncio.sleep(0.05)
        assert executor.in_flight == 2
        assert executor.saturated
        with pytest.raises(ExecutorSaturated):
            await executor.run(lambda: None)
        assert executor.in_flight == 2  # the rejected call took no slot
        release.set()
        await asyncio.gather(first, second)
        await asyncio.sleep(0.05)
        assert executor.in_flight == 0
        assert await executor.run(lambda: "again") == "again"


async def test_abandoned_call_keeps_its_slot_until_the_thread_finishes() -> None:
    release = threading.Event()
    async with SyncExecutor(workers=1, queue=0) as executor:
        task = asyncio.create_task(executor.run(release.wait, 5))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert executor.in_flight == 1  # the thread is still running
        with pytest.raises(ExecutorSaturated):
            await executor.run(lambda: None)
        release.set()
        await asyncio.sleep(0.1)
        assert executor.in_flight == 0


async def test_abandoned_call_closes_a_late_coroutine_result() -> None:
    started = threading.Event()
    release = threading.Event()
    results: list[Coroutine[Any, Any, int]] = []

    async with SyncExecutor(workers=1, queue=0) as executor:
        task = asyncio.create_task(
            executor.run(delayed_coroutine_call, started, release, results)
        )
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        release.set()
        await executor.drain(2)
        assert len(results) == 1
        assert inspect.getcoroutinestate(results[0]) == inspect.CORO_CLOSED


async def test_cancelled_call_cancels_the_work_it_handed_to_the_loop() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow() -> None:
        started.set()
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    def handler() -> None:
        blocking.run_sync(slow())

    async with SyncExecutor(workers=1, queue=0) as executor:
        task = asyncio.create_task(executor.run(handler))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.wait_for(cancelled.wait(), 1)
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0.1)
        assert executor.in_flight == 0


async def test_run_outside_lifespan_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="not running"):
        await SyncExecutor().run(lambda: None)


async def test_drain_waits_for_running_calls_and_reports_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    release = threading.Event()
    async with SyncExecutor(workers=1, queue=0) as executor:
        task = asyncio.create_task(executor.run(release.wait, 5))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        with caplog.at_level("WARNING", logger="hinoki.runtime"):
            await executor.drain(0.1)
        assert "still running" in caplog.text
        release.set()
        await executor.drain(2)
        assert executor.in_flight == 0


async def test_handed_off_futures_are_released_and_refused_after_cancel() -> None:
    seen: list[context.Handoff] = []
    release = threading.Event()
    refused: list[str] = []

    async def noop() -> None:
        return None

    def handler() -> None:
        handoff = context.handed_off.get()
        assert handoff is not None
        seen.append(handoff)
        for _ in range(3):
            blocking.run_sync(noop())
        assert not handoff.futures  # completed hand-offs are not retained
        release.wait(5)
        try:
            blocking.run_sync(noop())
        except RuntimeError as exc:
            refused.append(str(exc))

    async with SyncExecutor(workers=1, queue=0) as executor:
        task = asyncio.create_task(executor.run(handler))
        await asyncio.sleep(0.2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        release.set()
        await asyncio.sleep(0.2)
    assert seen[0].closed
    assert refused == ["the request ended; sync model calls are refused"]


async def test_executor_releases_capacity_when_submit_fails() -> None:
    class RejectingPool:
        _shutdown = False

        def submit(self, _call: Any) -> None:
            raise RuntimeError("submit rejected")

    executor = SyncExecutor(1, 0)
    executor._pool = RejectingPool()  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="submit rejected"):
        await executor.run(lambda: None)
    assert executor.in_flight == 0


@pytest.mark.parametrize(
    ("message", "shutdown"),
    [
        ("cannot schedule new futures after interpreter shutdown", False),
        ("pool shutdown", True),
    ],
)
async def test_shutdown_hook_uses_temporary_thread_after_pool_shutdown(
    message: str, shutdown: bool
) -> None:
    class RejectingPool:
        _shutdown = shutdown

        def submit(self, _call: Any) -> None:
            raise RuntimeError(message)

    executor = SyncExecutor(1, 0)
    executor._pool = RejectingPool()  # type: ignore[assignment]
    assert await executor.run_shutdown(lambda: "finished") == "finished"
    await asyncio.sleep(0)
    assert executor.in_flight == 0


async def test_shutdown_thread_failure_releases_handoff_and_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RejectingPool:
        _shutdown = True

        def submit(self, _call: Any) -> None:
            raise RuntimeError("pool shutdown")

    def fail(_call: Any) -> Future[Any]:
        raise OSError("thread unavailable")

    monkeypatch.setattr(SyncExecutor, "_run_in_shutdown_thread", staticmethod(fail))
    executor = SyncExecutor(1, 0)
    executor._pool = RejectingPool()  # type: ignore[assignment]
    with pytest.raises(OSError, match="thread unavailable"):
        await executor.run_shutdown(lambda: None)
    assert executor.in_flight == 0


def test_shutdown_thread_captures_callable_errors() -> None:
    def fail() -> None:
        raise ValueError("hook failed")

    future = SyncExecutor._run_in_shutdown_thread(fail)
    with pytest.raises(ValueError, match="hook failed"):
        future.result(timeout=1)


def test_abandoned_result_ignores_live_or_non_abandoned_futures() -> None:
    abandoned: AbandonedResult[int] = AbandonedResult()
    future: Future[int] = Future()
    abandoned.discard(future)
    future.set_result(1)
    abandoned.abandon()
    abandoned.discard(future)
    abandoned.discard(future)


async def test_submit_base_exception_releases_capacity() -> None:
    class Fatal(BaseException):
        pass

    class RejectingPool:
        _shutdown = False

        def submit(self, _call: Any) -> None:
            raise Fatal

    executor = SyncExecutor(1, 0)
    executor._pool = RejectingPool()  # type: ignore[assignment]
    with pytest.raises(Fatal):
        await executor.run(lambda: None)
    assert executor.in_flight == 0


def test_cancelled_shutdown_thread_future_does_not_run_callable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CancelBeforeRun:
        def __init__(self, target: Any, **_kwargs: Any) -> None:
            self.target = target

        def start(self) -> None:
            inspect.getclosurevars(self.target).nonlocals["future"].cancel()
            self.target()

    monkeypatch.setattr("hinoki.runtime.executor.threading.Thread", CancelBeforeRun)
    future = SyncExecutor._run_in_shutdown_thread(lambda: pytest.fail("was called"))
    assert future.cancelled()


async def test_call_cancelled_while_queued_never_runs_and_frees_its_slot() -> None:
    release = threading.Event()
    ran: list[str] = []
    async with SyncExecutor(workers=1, queue=1) as executor:
        first = asyncio.create_task(executor.run(release.wait, 5))
        queued = asyncio.create_task(executor.run(ran.append, "queued"))
        await asyncio.sleep(0.05)
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
        release.set()
        await first
        await executor.drain(2)
        assert ran == []
        assert executor.in_flight == 0


async def test_leaving_the_executor_cancels_queued_calls() -> None:
    release = threading.Event()
    executor = SyncExecutor(workers=1, queue=1)
    async with executor:
        running = asyncio.create_task(executor.run(release.wait, 5))
        queued = asyncio.create_task(executor.run(lambda: "late"))
        await asyncio.sleep(0.05)
    with pytest.raises(asyncio.CancelledError):
        await queued
    release.set()
    await running
    assert executor.in_flight == 0


async def test_exceptions_reach_the_caller() -> None:
    def stop() -> None:
        raise StopIteration

    async with SyncExecutor(workers=1, queue=0) as executor:
        with pytest.raises(ValueError, match="boom"):
            await executor.run(int, "boom")
        with pytest.raises(RuntimeError, match="StopIteration"):
            await executor.run(stop)
        assert await executor.run(lambda: "next") == "next"


async def test_a_shutdown_hook_still_runs_after_the_real_pool_is_stopped() -> None:
    executor = SyncExecutor(workers=1, queue=0)
    async with executor:
        pool = executor._pool
        assert pool is not None
        pool.shutdown(wait=True)
        with pytest.raises(RuntimeError, match="shutdown"):
            await executor.run(lambda: None)
        assert executor.in_flight == 0
        assert await executor.run_shutdown(lambda: "hook") == "hook"
