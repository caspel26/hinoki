"""Tasks with an owner: cancelled and awaited when the worker shuts down."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

log = logging.getLogger("hinoki.runtime")


class ManagedTasks:
    """Group of local tasks tied to the worker lifecycle.

    A *critical* task that fails marks the worker unhealthy (``failure`` is
    set and readiness turns false); a best-effort task only logs.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self.failure: BaseException | None = None
        self._closed = False

    def start_soon(
        self, coro: Coroutine[Any, Any, Any], *, name: str, critical: bool = False
    ) -> asyncio.Task[Any]:
        if self._closed:
            coro.close()
            raise RuntimeError("task group is closed")
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(lambda t: self._finished(t, critical))
        return task

    def _finished(self, task: asyncio.Task[Any], critical: bool) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        log.error("task %s failed", task.get_name(), exc_info=exc)
        if critical and self.failure is None:
            self.failure = exc

    @property
    def running(self) -> int:
        return len(self._tasks)

    async def aclose(self) -> None:
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
