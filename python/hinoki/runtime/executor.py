"""Bounded executor for sync handlers (ADR 0003)."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from types import TracebackType
from typing import ParamSpec, TypeVar

from hinoki import context

P = ParamSpec("P")
R = TypeVar("R")

log = logging.getLogger("hinoki.runtime")


class ExecutorSaturated(Exception):
    """All workers are busy and the queue is full: shed load, do not wait."""


class SyncExecutor:
    """Thread pool plus admission control, owned by one worker's event loop.

    At most ``workers + queue`` calls are admitted. A slot is released only
    when the thread really finishes, so a call abandoned by a timeout keeps
    occupying capacity (threads cannot be killed) and saturation stays visible.
    """

    def __init__(self, workers: int = 8, queue: int = 64) -> None:
        self.workers = workers
        self.capacity = workers + queue
        self.in_flight = 0
        self._pool: ThreadPoolExecutor | None = None

    async def __aenter__(self) -> SyncExecutor:
        self._pool = ThreadPoolExecutor(self.workers, thread_name_prefix="hinoki-sync")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            # Never block the loop on a stuck thread; queued work is dropped.
            pool.shutdown(wait=False, cancel_futures=True)

    @property
    def saturated(self) -> bool:
        return self.in_flight >= self.capacity

    async def run(self, fn: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
        pool = self._pool
        if pool is None:
            raise RuntimeError("executor is not running")
        if self.saturated:
            raise ExecutorSaturated
        loop = asyncio.get_running_loop()
        self.in_flight += 1
        # Copy on the loop thread so request-scoped ContextVars follow the call;
        # the loop travels with it so sync model methods can reach the database.
        scope = contextvars.copy_context()
        scope.run(context.event_loop.set, loop)
        handed: list[Future[object]] = []
        scope.run(context.handed_off.set, handed)
        call = functools.partial(scope.run, fn, *args, **kwargs)
        try:
            future = pool.submit(call)
        except BaseException:
            self.in_flight -= 1
            raise
        future.add_done_callback(functools.partial(self._notify, loop))
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            # The deadline fired: stop work the thread handed to the loop, so a
            # pending query cannot pin the connection past the request.
            for pending in handed:
                pending.cancel()
            raise

    async def drain(self, timeout: float) -> None:
        """Wait, bounded, for running sync calls (even abandoned ones) to end."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.in_flight and loop.time() < deadline:
            await asyncio.sleep(0.02)
        if self.in_flight:
            log.warning("%d sync call(s) still running after drain", self.in_flight)

    def _notify(self, loop: asyncio.AbstractEventLoop, _future: object) -> None:
        """Runs on the worker thread: hand the slot release back to the loop."""
        with contextlib.suppress(RuntimeError):  # the loop already closed
            loop.call_soon_threadsafe(self._release)

    def _release(self) -> None:
        self.in_flight -= 1
