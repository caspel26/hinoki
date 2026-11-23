import asyncio
from collections.abc import AsyncIterator, Awaitable, Coroutine, Sequence
from contextlib import asynccontextmanager
from typing import cast

import pytest

from hinoki import Module, ModuleContext, RuntimeConfig
from hinoki.errors import ShutdownError, StartupError
from hinoki.runtime.executor import SyncExecutor
from hinoki.runtime.lifespan import Hook, Lifespan


class Recording(Module):
    """Module that records start/stop events; behaviour is set per instance."""

    def __init__(
        self,
        events: list[str],
        *,
        fail_start: bool = False,
        fail_stop: bool = False,
        hang_start: bool = False,
    ) -> None:
        self.events = events
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.hang_start = hang_start

    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        self.events.append(f"start:{self.name}")
        if self.fail_start:
            raise RuntimeError("secret-value-123")
        if self.hang_start:
            await asyncio.sleep(60)
        try:
            yield
        finally:
            self.events.append(f"stop:{self.name}")
            if self.fail_stop:
                raise RuntimeError(f"stop-{self.name}")


class HangsOnExit(Recording):
    name = "hang"

    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        self.events.append("start:hang")
        try:
            yield
        finally:
            await asyncio.sleep(60)


class First(Recording):
    name = "a"


class Second(Recording):
    name = "b"
    requires = (First,)


class Third(Recording):
    name = "c"
    requires = (Second,)


class Fourth(Recording):
    name = "d"
    requires = (Third,)


def build(modules: Sequence[Module], **config: float) -> Lifespan:
    runtime = RuntimeConfig(**config)  # type: ignore[arg-type]
    return Lifespan(modules, ModuleContext(), SyncExecutor(2, 2), runtime)


async def test_startup_in_dependency_order_and_reverse_shutdown() -> None:
    events: list[str] = []
    life = build([Second(events), First(events)])
    assert not life.ready
    await life.startup()
    assert life.ready
    await life.shutdown()
    assert not life.ready
    await life.shutdown()  # idempotent
    assert events == ["start:a", "start:b", "stop:b", "stop:a"]


async def test_failure_in_k_closes_only_entered_modules_in_reverse() -> None:
    events: list[str] = []
    modules = [
        First(events),
        Second(events),
        Third(events, fail_start=True),
        Fourth(events),
    ]
    life = build(modules)
    with pytest.raises(StartupError) as info:
        await life.startup()
    assert not life.ready
    assert events == ["start:a", "start:b", "start:c", "stop:b", "stop:a"]
    assert info.value.module == "c"
    assert "secret-value-123" not in str(info.value)
    assert "RuntimeError" in str(info.value)


async def test_startup_timeout_is_a_startup_failure_with_cleanup() -> None:
    events: list[str] = []
    life = build([First(events), Second(events, hang_start=True)], startup_timeout=0.2)
    with pytest.raises(StartupError) as info:
        await life.startup()
    assert info.value.module == "b"
    assert isinstance(info.value.cause, TimeoutError)
    assert events[-1] == "stop:a"


async def test_cleanup_error_does_not_prevent_other_cleanups() -> None:
    events: list[str] = []
    life = build([First(events), Second(events, fail_stop=True)])
    await life.startup()
    with pytest.raises(ShutdownError):
        await life.shutdown()
    assert events == ["start:a", "start:b", "stop:b", "stop:a"]


async def forever(cancelled: list[str]) -> None:
    try:
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        cancelled.append("worker")
        raise


async def fail() -> None:
    raise RuntimeError("x")


async def test_managed_tasks_are_cancelled_and_awaited_on_shutdown() -> None:
    cancelled: list[str] = []
    life = build([])
    await life.startup()
    life.ctx.tasks.start_soon(forever(cancelled), name="background")
    await asyncio.sleep(0)
    assert life.ctx.tasks.running == 1
    await life.shutdown()
    assert life.ctx.tasks.running == 0
    assert cancelled == ["worker"]


async def test_critical_task_failure_makes_worker_unready() -> None:
    life = build([])
    await life.startup()
    life.ctx.tasks.start_soon(fail(), name="best-effort")
    await asyncio.sleep(0.01)
    assert life.ready
    life.ctx.tasks.start_soon(fail(), name="critical", critical=True)
    await asyncio.sleep(0.01)
    assert not life.ready
    await life.shutdown()


def test_context_registry_is_typed_and_rejects_duplicates() -> None:
    ctx = ModuleContext()
    ctx.provide(str, "svc")
    assert ctx.get(str) == "svc"
    with pytest.raises(LookupError):
        ctx.get(int)
    with pytest.raises(Exception, match="already provided"):
        ctx.provide(str, "again")


async def stubborn(stop: asyncio.Event) -> None:
    """Swallows cancellation until ``stop`` is set (so the test can clean up)."""
    while not stop.is_set():
        try:
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            continue


async def test_hung_task_and_hung_module_do_not_skip_other_cleanup() -> None:
    events: list[str] = []
    life = build([First(events), HangsOnExit(events)], shutdown_timeout=0.2)
    await life.startup()
    stop = asyncio.Event()
    life.ctx.tasks.start_soon(stubborn(stop), name="stubborn")
    await asyncio.sleep(0)
    with pytest.raises(ShutdownError) as info:
        await life.shutdown()
    stop.set()
    assert events[-1] == "stop:a", "teardown continued after the hung steps"
    assert len(info.value.causes) >= 2
    assert all(isinstance(c, TimeoutError) for c in info.value.causes)


async def test_lifespan_can_run_again_after_shutdown() -> None:
    events: list[str] = []
    life = build([First(events)])
    await life.startup()
    life.ctx.provide(str, "first")
    life.ctx.tasks.start_soon(asyncio.sleep(60), name="bg")
    await life.shutdown()
    await life.startup()
    life.ctx.provide(str, "second")  # registry was cleared
    life.ctx.tasks.start_soon(asyncio.sleep(60), name="bg")
    await life.shutdown()
    assert events == ["start:a", "stop:a", "start:a", "stop:a"]


async def test_duplicate_startup_is_a_startup_error_not_a_crash() -> None:
    life = build([])
    await life.startup()
    with pytest.raises(StartupError, match="<lifespan>"):
        await life.startup()
    await life.shutdown()


async def test_an_atomic_action_without_a_database_fails_startup() -> None:
    events: list[str] = []
    life = Lifespan(
        [First(events)],
        ModuleContext(),
        SyncExecutor(2, 2),
        RuntimeConfig(),
        needs_database=True,
    )
    with pytest.raises(StartupError, match="<transactions>") as failed:
        await life.startup()
    assert "atomic=True" in str(failed.value.cause)
    assert events == ["start:a", "stop:a"]  # what was entered is closed again
    assert not life.ready


def test_generator_finalization_without_a_loop_skips_async_cleanup() -> None:
    life = build([])
    assert not life._can_cleanup_after_startup_error(GeneratorExit())


async def test_startup_reraises_generator_exit_when_cleanup_context_is_unsafe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class ExitsDuringEnter(Recording):
        name = "exit"

        @asynccontextmanager
        async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
            raise GeneratorExit("context manager is being finalized")
            yield

    life = build([First(events), ExitsDuringEnter(events)])
    monkeypatch.setattr(life, "_can_cleanup_after_startup_error", lambda _exc: False)
    with pytest.raises(GeneratorExit, match="being finalized"):
        await life.startup()

    assert events == ["start:a"]
    await life.shutdown()
    assert events == ["start:a", "stop:a"]


async def test_sync_hook_returning_an_awaitable_is_awaited() -> None:
    events: list[str] = []

    async def finish() -> None:
        events.append("finished")

    def wrapped(_ctx: ModuleContext) -> Awaitable[None]:
        return finish()

    life = build([])
    life.on_startup = (wrapped,)
    await life.startup()
    await life.shutdown()
    assert events == ["finished"]


async def test_shutdown_hook_cancellation_is_collected_and_cleanup_continues() -> None:
    async def cancelled(_ctx: ModuleContext) -> None:
        raise asyncio.CancelledError

    events: list[str] = []
    life = build([First(events)])
    life.on_shutdown = (cancelled,)
    await life.startup()
    with pytest.raises(ShutdownError) as info:
        await life.shutdown()
    assert len(info.value.causes) == 1
    assert isinstance(info.value.causes[0], asyncio.CancelledError)
    assert events == ["start:a", "stop:a"]


async def test_shutdown_propagates_external_hook_cancellation_after_cleanup() -> None:
    entered = asyncio.Event()

    async def slow(_ctx: ModuleContext) -> None:
        entered.set()
        await asyncio.Event().wait()

    events: list[str] = []
    life = build([First(events)])
    life.on_shutdown = (slow,)
    await life.startup()
    task = asyncio.create_task(life.shutdown())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["start:a", "stop:a"]


async def test_hook_returning_a_plain_value_fails_startup() -> None:
    def invalid(_ctx: ModuleContext) -> str:
        return "not allowed"

    life = build([])
    life.on_startup = (cast(Hook, invalid),)
    with pytest.raises(StartupError) as info:
        await life.startup()
    assert isinstance(info.value.cause, TypeError)
    assert "must return None or an awaitable" in str(info.value.cause)


async def test_cleanup_step_records_internal_cancellation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def cancelled() -> None:
        raise asyncio.CancelledError

    life = build([])
    causes: list[BaseException] = []
    with caplog.at_level("ERROR", logger="hinoki.runtime"):
        await life._step(cancelled(), causes, label="test cleanup")
    assert len(causes) == 1 and isinstance(causes[0], asyncio.CancelledError)
    assert "error during test cleanup: CancelledError" in caplog.text


async def test_finish_close_preserves_external_cancellation_until_cleanup_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    life = build([])
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_close() -> list[BaseException]:
        entered.set()
        await release.wait()
        return []

    monkeypatch.setattr(life, "_close", slow_close)
    causes: list[BaseException] = []
    task = asyncio.create_task(life._finish_close(causes, None))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    cancelled = await task
    assert isinstance(cancelled, asyncio.CancelledError)


async def test_close_after_cancellation_finishes_a_completed_close_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    life = build([])
    finished = asyncio.Event()

    async def close() -> list[BaseException]:
        finished.set()
        return []

    monkeypatch.setattr(life, "_close", close)
    task = asyncio.create_task(life._close_after_cancellation())
    await finished.wait()
    task.cancel()
    await task


async def test_cleanup_step_propagates_external_cancellation() -> None:
    life = build([])
    started = asyncio.Event()

    async def wait() -> None:
        started.set()
        await asyncio.Event().wait()

    causes: list[BaseException] = []
    task = asyncio.create_task(life._step(wait(), causes))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert causes == []


async def test_shutdown_hook_runner_records_non_external_cancelled_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    life = build([])

    async def cancelled_step(
        work: Coroutine[object, object, object], *_args: object, **_kwargs: object
    ) -> None:
        work.close()
        raise asyncio.CancelledError

    async def hook(_ctx: ModuleContext) -> None:
        return None

    monkeypatch.setattr(life, "_step", cancelled_step)
    monkeypatch.setattr(life, "_is_external_cancellation", lambda _exc: False)
    life.on_shutdown = (hook,)
    causes: list[BaseException] = []
    result = await life._run_shutdown_hooks(causes)
    assert result is None
    assert len(causes) == 1 and isinstance(causes[0], asyncio.CancelledError)
