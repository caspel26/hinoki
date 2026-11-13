import asyncio
import contextvars
import threading
import time

import pytest

from hinoki import blocking, context
from hinoki.runtime.executor import ExecutorSaturated, SyncExecutor

var: contextvars.ContextVar[str] = contextvars.ContextVar("var", default="unset")


def read_context() -> tuple[str, str]:
    return var.get(), threading.current_thread().name


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
