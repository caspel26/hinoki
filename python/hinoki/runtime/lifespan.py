"""Worker lifespan coordinator: ordered startup, reverse cleanup, deadlines."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Sequence
from contextlib import AbstractAsyncContextManager

from hinoki.db import Database
from hinoki.errors import ShutdownError, StartupError
from hinoki.modules import Module, ModuleContext, order_modules
from hinoki.runtime.config import RuntimeConfig
from hinoki.runtime.executor import SyncExecutor

log = logging.getLogger("hinoki.runtime")


class Lifespan:
    """Opens the worker runtime, then each module in dependency order.

    If module *k* fails, only what was actually entered is closed, in reverse.
    Every cleanup step runs under its own deadline and its own ``try``, so a
    hung or failing step never prevents the next one. ``ready`` is true only
    between a successful startup and the start of shutdown, and is false after
    a critical managed task failed.
    """

    def __init__(
        self,
        modules: Sequence[Module],
        ctx: ModuleContext,
        executor: SyncExecutor,
        config: RuntimeConfig,
        *,
        needs_database: bool = False,
    ) -> None:
        self.modules = order_modules(modules)
        self.ctx = ctx
        self.executor = executor
        self.config = config
        self.needs_database = needs_database
        self._entered: list[AbstractAsyncContextManager[object]] = []
        self._running = False
        self._started = False

    @property
    def ready(self) -> bool:
        return self._started and self.ctx.tasks.failure is None

    async def startup(self) -> None:
        if self._running:
            raise StartupError("<lifespan>", RuntimeError("lifespan already started"))
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
        except BaseException as exc:
            await self._close()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise StartupError(current, exc) from exc
        self._started = True

    def _check_database(self) -> None:
        """Actions with ``transaction=True`` need a provided ``Database``."""
        if self.needs_database:
            try:
                self.ctx.get(Database)
            except LookupError:
                raise RuntimeError(
                    "an action uses transaction=True but no module provided a Database"
                ) from None

    async def shutdown(self) -> None:
        """Idempotent. Raises ``ShutdownError`` after attempting every step."""
        if not self._running:
            return
        self._started = False
        causes = await self._close()
        if causes:
            raise ShutdownError(causes)

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

    async def _step(self, work: Awaitable[object], causes: list[BaseException]) -> None:
        try:
            async with asyncio.timeout(self.config.shutdown_timeout):
                await work
        except Exception as exc:
            log.exception("error while closing worker resources")
            causes.append(exc)
