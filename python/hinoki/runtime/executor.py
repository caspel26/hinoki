"""Bounded executor for sync handlers (ADR 0003)."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import inspect
import logging
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from types import TracebackType
from typing import Any, ParamSpec, TypeVar

from hinoki import context

P = ParamSpec("P")
R = TypeVar("R")

log = logging.getLogger("hinoki.runtime")
CANCELLED = asyncio.CancelledError()


class ExecutorSaturated(Exception):
    """All workers are busy and the queue is full: shed load, do not wait."""


class AbandonedResult[T]:
    """Close a coroutine result if its executor request has been cancelled."""

    def __init__(self) -> None:
        self._abandoned = threading.Event()
        self._lock = threading.Lock()
        self._discarded = False

    def abandon(self) -> None:
        self._abandoned.set()

    def discard(self, done: Future[T]) -> None:
        if not self._abandoned.is_set() or not done.done():
            return
        with self._lock:
            if self._discarded:
                return
            self._discarded = True
        try:
            result = done.result()
        except BaseException:
            return
        if inspect.iscoroutine(result):
            result.close()


Job = tuple[
    Callable[[], object],
    asyncio.AbstractEventLoop,
    "asyncio.Future[Any]",
    Callable[["asyncio.Future[Any]", object, "BaseException | None"], None],
]


class WorkerPool:
    """Plain worker threads on one queue. A finished call reaches its event
    loop with a single ``call_soon_threadsafe``; there is no
    ``concurrent.futures`` future per call, which is what the standard pool
    adds to every sync handler.

    Workers are daemon threads: an idle one never holds the interpreter open,
    and the lifespan has already drained running calls by the time it exits."""

    def __init__(self, workers: int) -> None:
        self._jobs: queue.SimpleQueue[Job | None] = queue.SimpleQueue()
        self._shutdown = False
        self._threads = [
            threading.Thread(
                target=self._work, name=f"hinoki-sync_{index}", daemon=True
            )
            for index in range(workers)
        ]
        for thread in self._threads:
            thread.start()

    def submit(self, job: Job) -> None:
        if self._shutdown:
            raise RuntimeError("cannot schedule new futures after shutdown")
        self._jobs.put(job)

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        """Stop the workers; with ``cancel_futures`` the queued calls are
        cancelled. Running calls finish (threads cannot be killed)."""
        self._shutdown = True
        if cancel_futures:
            self._cancel_queued()
        for _ in self._threads:
            self._jobs.put(None)
        if wait:
            for thread in self._threads:
                thread.join()

    def _cancel_queued(self) -> None:
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                return
            if job is not None:
                _call, loop, future, finish = job
                with contextlib.suppress(RuntimeError):  # the loop already closed
                    loop.call_soon_threadsafe(finish, future, None, CANCELLED)

    def _work(self) -> None:
        get = self._jobs.get
        while (job := get()) is not None:
            call, loop, future, finish = job
            result: object = None
            failure: BaseException | None = None
            # A call cancelled while still queued never runs.
            if future.cancelled():
                failure = CANCELLED
            else:
                try:
                    result = call()
                except BaseException as exc:
                    failure = exc
            with contextlib.suppress(RuntimeError):  # the loop already closed
                loop.call_soon_threadsafe(finish, future, result, failure)
            del job, call, future, result, failure


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
        self._pool: WorkerPool | None = None

    async def __aenter__(self) -> SyncExecutor:
        self._pool = WorkerPool(self.workers)
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
        return await self._run(fn, False, *args, **kwargs)

    async def run_shutdown(
        self, fn: Callable[P, R], *args: P.args, **kwargs: P.kwargs
    ) -> R:
        """Run a shutdown callable, with a temporary-thread exit fallback."""
        return await self._run(fn, True, *args, **kwargs)

    async def _run(
        self,
        fn: Callable[P, R],
        allow_shutdown_thread: bool,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        pool = self._pool
        if pool is None:
            raise RuntimeError("executor is not running")
        if self.saturated:
            raise ExecutorSaturated
        loop = asyncio.get_running_loop()
        # Copy on the loop thread so request-scoped ContextVars follow the call;
        # the loop travels with it so sync model methods can reach the database.
        scope = contextvars.copy_context()
        handed = context.Handoff()
        scope.run(self._attach, loop, handed)
        call = functools.partial(scope.run, fn, *args, **kwargs)
        future: asyncio.Future[R] = loop.create_future()
        self.in_flight += 1
        try:
            pool.submit((call, loop, future, self._finish))
        except RuntimeError as exc:
            return await self._run_fallback(
                pool, call, handed, exc, allow_shutdown_thread
            )
        except BaseException:
            self.in_flight -= 1
            raise
        try:
            return await future
        except asyncio.CancelledError:
            # The deadline fired: stop work the thread handed to the loop, so a
            # pending query cannot pin the connection past the request. The slot
            # is released, and a late coroutine result closed, when the thread
            # really finishes (see ``_finish``).
            handed.close()
            raise

    @staticmethod
    def _attach(loop: asyncio.AbstractEventLoop, handed: context.Handoff) -> None:
        context.event_loop.set(loop)
        context.handed_off.set(handed)

    def _finish(
        self, future: asyncio.Future[Any], result: object, failure: BaseException | None
    ) -> None:
        """Runs on the loop when a call ended (or was dropped from the queue)."""
        self.in_flight -= 1
        if future.cancelled():
            if inspect.iscoroutine(result):
                result.close()  # no request task is left to own it
        elif failure is None:
            future.set_result(result)
        elif isinstance(failure, asyncio.CancelledError):
            future.cancel()
        else:
            future.set_exception(failure)

    async def _run_fallback(
        self,
        pool: WorkerPool,
        call: Callable[[], R],
        handed: context.Handoff,
        error: RuntimeError,
        allow_shutdown_thread: bool,
    ) -> R:
        """The pool refused the call: only a shutdown hook may go on, in a
        temporary thread."""
        interpreter_exit = "interpreter shutdown" in str(error)
        if not (
            allow_shutdown_thread
            and (getattr(pool, "_shutdown", False) or interpreter_exit)
        ):
            self.in_flight -= 1
            raise error
        loop = asyncio.get_running_loop()
        try:
            future = self._run_in_shutdown_thread(call)
        except BaseException:
            self.in_flight -= 1
            handed.close()
            raise
        abandoned: AbandonedResult[R] = AbandonedResult()
        future.add_done_callback(functools.partial(self._notify, loop))
        future.add_done_callback(abandoned.discard)
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            abandoned.abandon()
            abandoned.discard(future)
            handed.close()
            raise

    @staticmethod
    def _run_in_shutdown_thread(call: Callable[[], R]) -> Future[R]:
        """Run a shutdown call when ThreadPoolExecutor rejects new work."""
        future: Future[R] = Future()

        def invoke() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                future.set_result(call())
            except BaseException as exc:
                future.set_exception(exc)

        threading.Thread(target=invoke, name="hinoki-shutdown", daemon=True).start()
        return future

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
