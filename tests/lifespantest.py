import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import pytest

from hinoki import Module, ModuleContext, RuntimeConfig
from hinoki.errors import ShutdownError, StartupError
from hinoki.runtime.executor import SyncExecutor
from hinoki.runtime.lifespan import Lifespan


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
