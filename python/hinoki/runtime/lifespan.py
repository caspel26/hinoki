"""Worker lifespan coordinator: ordered startup, reverse cleanup, deadlines."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import AsyncExitStack

from hinoki.errors import ShutdownError, StartupError
from hinoki.modules import Module, ModuleContext, order_modules
from hinoki.runtime.config import RuntimeConfig
from hinoki.runtime.executor import SyncExecutor

log = logging.getLogger("hinoki.runtime")


class Lifespan:
    """Opens the worker runtime, then each module in dependency order.

    If module *k* fails, only what was actually entered is closed, in reverse.
    ``ready`` is true only between a successful startup and the start of
    shutdown, and is false after a critical managed task failed.
    """

    def __init__(
        self,
        modules: Sequence[Module],
        ctx: ModuleContext,
        executor: SyncExecutor,
        config: RuntimeConfig,
    ) -> None:
        self.modules = order_modules(modules)
        self.ctx = ctx
        self.executor = executor
        self.config = config
        self._stack: AsyncExitStack | None = None
        self._started = False

    @property
    def ready(self) -> bool:
        return (
            self._started
            and self.ctx.tasks.failure is None
            and not self.executor.saturated
        )

    async def startup(self) -> None:
        if self._stack is not None:
            raise RuntimeError("lifespan already started")
        stack = self._stack = AsyncExitStack()
        current = "<runtime>"
        try:
            async with asyncio.timeout(self.config.startup_timeout):
                await stack.enter_async_context(self.executor)
                for module in self.modules:
                    current = module.name
                    await stack.enter_async_context(module.lifespan(self.ctx))
        except BaseException as exc:
            await self._unwind(stack)
            self._stack = None
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise StartupError(current, exc) from exc
        self._started = True

    async def shutdown(self) -> None:
        """Idempotent. Raises ``ShutdownError`` after attempting every step."""
        stack, self._stack = self._stack, None
        self._started = False
        if stack is None:
            return
        try:
            await self._unwind(stack, raise_errors=True)
        except Exception as exc:
            raise ShutdownError([exc]) from exc

    async def _unwind(self, stack: AsyncExitStack, raise_errors: bool = False) -> None:
        try:
            async with asyncio.timeout(self.config.shutdown_timeout):
                await self.ctx.tasks.aclose()
                await stack.aclose()
        except Exception:
            log.exception("error while closing worker resources")
            if raise_errors:
                raise
