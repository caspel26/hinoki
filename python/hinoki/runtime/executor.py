"""Bounded executor for sync handlers (ADR 0003)."""

from __future__ import annotations

import asyncio
import contextvars
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from types import TracebackType
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")


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
        # Copy on the loop thread so request-scoped ContextVars follow the call.
        call = functools.partial(contextvars.copy_context().run, fn, *args, **kwargs)
        try:
            future = pool.submit(call)
        except BaseException:
            self.in_flight -= 1
            raise
        future.add_done_callback(lambda _: loop.call_soon_threadsafe(self._release))
        return await asyncio.wrap_future(future)

    def _release(self) -> None:
        self.in_flight -= 1
