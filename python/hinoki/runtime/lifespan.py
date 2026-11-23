"""Worker lifespan coordinator: ordered startup, reverse cleanup, deadlines."""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager

from hinoki import context
from hinoki.db import Database
from hinoki.errors import STARTUP_HOOK, ShutdownError, StartupError
from hinoki.events import EventBus, events_var
from hinoki.mail import Mailer, mailer_var
from hinoki.modules import Module, ModuleContext, order_modules
from hinoki.orm import database_var
from hinoki.runtime.config import RuntimeConfig
from hinoki.runtime.executor import SyncExecutor

log = logging.getLogger("hinoki.runtime")

Hook = Callable[[ModuleContext], Awaitable[None] | None]
"""An application startup or shutdown hook: ``async def`` or plain ``def``."""


class _HookRaised(Exception):
    """Carry a hook's BaseException without letting it escape an asyncio task."""

    def __init__(self, cause: BaseException) -> None:
        self.cause = cause


def hook_name(hook: Hook) -> str:
    while isinstance(hook, functools.partial):
        hook = hook.func
    return getattr(hook, "__name__", type(hook).__name__)


class Lifespan:
    """Opens the worker runtime, then each module in dependency order, then
    runs the application's startup hooks.

    If module *k* or a startup hook fails, only what was actually entered is
    closed, in reverse, and no shutdown hook runs. After a completed startup,
    shutdown runs the shutdown hooks first, while every resource is still open.
    Every cleanup step runs under its own deadline and its own ``try``, so a
    hung or failing step never prevents the next one. ``ready`` is true only
    between a successful startup and the start of shutdown, and is false after
    a critical managed task failed.
    """

    def __init__(  # noqa: PLR0913 - the runtime pieces ride along
        self,
        modules: Sequence[Module],
        ctx: ModuleContext,
        executor: SyncExecutor,
        config: RuntimeConfig,
        *,
        needs_database: bool = False,
        on_startup: Sequence[Hook] = (),
        on_shutdown: Sequence[Hook] = (),
        events: EventBus | None = None,
    ) -> None:
        self.modules = order_modules(modules)
        self.ctx = ctx
        self.events = events
        self.executor = executor
        self.config = config
        self.needs_database = needs_database
        self.on_startup = tuple(on_startup)
        self.on_shutdown = tuple(on_shutdown)
        self._entered: list[AbstractAsyncContextManager[object]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._started = False

    @property
    def ready(self) -> bool:
        return self._started and self.ctx.tasks.failure is None

    async def startup(self) -> None:
        if self._running:
            raise StartupError("<lifespan>", RuntimeError("lifespan already started"))
        self._loop = asyncio.get_running_loop()
        self._running = True
        self.ctx.reset()
        current = "<runtime>"
        try:
            async with asyncio.timeout(self.config.startup_timeout):
                await self._enter(self.executor)
                for module in self.modules:
                    current = module.name
                    await self._enter(module.lifespan(self.ctx))
                current = "<transactions>"
                self._check_database()
                for hook in self.on_startup:
                    current = STARTUP_HOOK + hook_name(hook)
                    await self._call(hook)
        except BaseException as exc:
            if not self._can_cleanup_after_startup_error(exc):
                raise exc from None
            await self._close_after_cancellation()
            if self._is_external_cancellation(exc):
                raise
            cause = exc.cause if isinstance(exc, _HookRaised) else exc
            raise StartupError(current, cause) from exc
        self._started = True

    def _can_cleanup_after_startup_error(self, exc: BaseException) -> bool:
        """Avoid async cleanup when this coroutine is finalized off-loop."""
        if not isinstance(exc, GeneratorExit):
            return True
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        return running_loop is self._loop

    def _check_database(self) -> None:
        """Actions with ``atomic=True`` need a provided ``Database``."""
        if self.needs_database:
            try:
                self.ctx.get(Database)
            except LookupError:
                raise RuntimeError(
                    "an action uses atomic=True but no module provided a Database"
                ) from None

    async def shutdown(self) -> None:
        """Idempotent. Raises ``ShutdownError`` after attempting every step."""
        if not self._running:
            return
        completed, self._started = self._started, False
        causes: list[BaseException] = []
        cancelled = await self._run_shutdown_hooks(causes) if completed else None
        cancelled = await self._finish_close(causes, cancelled)
        if cancelled is not None:
            raise cancelled
        # The hooks append to the list.
        if causes:  # NOSONAR
            raise ShutdownError(causes)

    async def _run_shutdown_hooks(
        self, causes: list[BaseException]
    ) -> asyncio.CancelledError | None:
        cancelled: asyncio.CancelledError | None = None
        for hook in self.on_shutdown:
            try:
                await self._step(
                    self._call(hook, shutdown=True),
                    causes,
                    label=f"shutdown hook {hook_name(hook)!r}",
                )
            # Kept and re-raised once the hooks have run.
            except asyncio.CancelledError as exc:  # NOSONAR
                if self._is_external_cancellation(exc):
                    cancelled = exc
                else:
                    causes.append(exc)
        return cancelled

    async def _finish_close(
        self,
        causes: list[BaseException],
        cancelled: asyncio.CancelledError | None,
    ) -> asyncio.CancelledError | None:
        close_task = asyncio.create_task(self._close())
        while True:
            try:
                causes.extend(await asyncio.shield(close_task))
                return cancelled
            # Kept and re-raised once the close has finished.
            except asyncio.CancelledError as exc:  # NOSONAR
                if cancelled is None:
                    cancelled = exc

    async def _call(self, hook: Hook, *, shutdown: bool = False) -> None:
        """Await an ``async def`` hook; run a plain ``def`` one on the bounded
        sync executor, as sync handlers run."""
        # Models reach the worker's Database in a hook as they do in a request.
        token = database_var.set(self.ctx.find(Database))
        events_token = events_var.set(self.events)
        mailer_token = mailer_var.set(self.ctx.find(Mailer))
        run_sync = self.executor.run_shutdown if shutdown else self.executor.run
        runner_token = context.sync_runner.set(run_sync)
        try:
            try:
                if inspect.iscoroutinefunction(hook):
                    result = await hook(self.ctx)
                else:
                    result = await run_sync(hook, self.ctx)
                    if inspect.isawaitable(result):  # a sync wrapper around a coroutine
                        result = await result
                if result is not None:
                    raise TypeError(
                        f"hook {hook_name(hook)!r} must return None or an awaitable"
                    )
            except BaseException as exc:
                if self._is_external_cancellation(exc):
                    raise
                raise _HookRaised(exc) from None
        finally:
            context.sync_runner.reset(runner_token)
            mailer_var.reset(mailer_token)
            events_var.reset(events_token)
            database_var.reset(token)

    async def _enter(self, manager: AbstractAsyncContextManager[object]) -> None:
        await manager.__aenter__()
        self._entered.append(manager)

    async def _close(self) -> list[BaseException]:
        causes: list[BaseException] = []
        await self._step(self.ctx.tasks.aclose(), causes)
        await self._step(self.executor.drain(self.config.shutdown_timeout), causes)
        while self._entered:
            manager = self._entered.pop()
            await self._step(manager.__aexit__(None, None, None), causes)
        self.ctx.reset()
        self._running = False
        return causes

    async def _close_after_cancellation(self) -> None:
        """Finish resource cleanup even when startup itself was cancelled."""
        task = asyncio.create_task(self._close())
        while True:
            try:
                await asyncio.shield(task)
                return
            # The caller of the startup re-raises after the cleanup.
            except asyncio.CancelledError:  # NOSONAR
                if task.done():
                    task.result()
                    return

    @staticmethod
    def _is_external_cancellation(exc: BaseException) -> bool:
        if not isinstance(exc, asyncio.CancelledError):
            return False
        task = asyncio.current_task()
        return task is not None and task.cancelling() > 0

    async def _step(
        self,
        work: Awaitable[object],
        causes: list[BaseException],
        *,
        label: str = "worker resource cleanup",
    ) -> None:
        try:
            async with asyncio.timeout(self.config.shutdown_timeout):
                await work
        except asyncio.CancelledError as exc:
            if self._is_external_cancellation(exc):
                raise
            log.error("error during %s: CancelledError", label)
            causes.append(exc)
        except _HookRaised as exc:
            log.error("error during %s: %s", label, type(exc.cause).__name__)
            causes.append(exc.cause)
        except BaseException as exc:
            log.exception("error during %s", label)
            causes.append(exc)
