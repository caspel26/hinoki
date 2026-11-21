"""The native lane: an async handler driven from Rust instead of an asyncio task.

A route is served here only when everything it does is known at startup: an
``async def`` that ``nativeproof`` shows awaits nothing but stock ORM reads;
an ``atomic`` route runs it inside the transaction the lane opens. A guarded
route (access policy, rate limit, ``Query`` schema or several renderers) runs
its guards first, and a body method reads and checks its body, inside the same
coroutine; the body read is a native wait too. The coroutine runs without a
task: the first step on the loop thread, each database wait on the native
runtime, every later step back on the loop thread (so a task the handler
spawns sees the loop). The request timeout is the driver's deadline.

A request the lane cannot serve (an input error, a route outside it) takes the
asyncio path. A coroutine that awaits something only asyncio can run (a
database backend without native waits) is handed over to a task and continues
there with what is left of the timeout.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import types
from collections.abc import Awaitable, Callable, Generator
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from hinoki import core
from hinoki.db import NATIVE_OWNER, current, current_owner
from hinoki.runtime.binding import BODY_METHODS, Binding
from hinoki.runtime.nativeproof import native_refusal
from hinoki.runtime.pipeline import NativeBody
from hinoki.runtime.responses import HTTPError, Response
from hinoki.runtime.servercore import native_handler_eligible, route_guarded

if TYPE_CHECKING:
    import contextvars

    from hinoki.runtime.pipeline import BodySource, Pipeline

log = logging.getLogger("hinoki.runtime")


def needs_request_data(binding: Binding) -> bool:
    """Whether the route reads the headers, the peer or the body itself."""
    return binding.route.method in BODY_METHODS or route_guarded(binding)


def lane_refusal(binding: Binding) -> str | None:
    """Why the native lane does not serve this route, or ``None`` when it does."""
    route = binding.route
    reason = None
    if binding.streams:
        reason = "streamed response"
    elif not binding.is_async:
        reason = "not an async def handler"
    elif not native_handler_eligible(binding):
        reason = "not natively eligible"
    return reason or native_refusal(route.handler)


def timeout_response() -> Response:
    return HTTPError(HTTPStatus.GATEWAY_TIMEOUT, "timeout").response


def adopt_transaction() -> None:
    """The transaction a driven handler opened passes to the task now running
    it, so a savepoint opened after the handover is the owner's, not a stranger's."""
    active = current.get()
    if active is not None and active.owner is NATIVE_OWNER:
        active.owner = current_owner()


@types.coroutine
def resume(coro: Any, yielded: Any) -> Generator[Any, Any, Any]:
    """Continue ``coro`` in an asyncio task after it yielded ``yielded`` (a
    future or bare yield only asyncio can wait for): hand it to the task, and
    send or throw what comes back into ``coro``. The task adopts the open
    transaction."""
    adopt_transaction()
    while True:
        try:
            sent = yield yielded
        except GeneratorExit:
            coro.close()
            raise
        except BaseException as exc:
            step = coro.throw
            argument: Any = exc
        else:
            step = coro.send
            argument = sent
        try:
            yielded = step(argument)
        except StopIteration as stop:
            return stop.value


class Run:
    """One request in the lane."""

    __slots__ = ("binding", "ctx", "handle", "request", "rid")

    def __init__(
        self,
        request: core.PreparedRequest,
        binding: Binding,
        ctx: contextvars.Context,
    ) -> None:
        self.request = request
        self.binding = binding
        self.ctx = ctx
        self.rid = request.request_id
        self.handle: core._NativeRun | None = None


class NativeLane:
    """Starts requests of the selected routes and answers them."""

    def __init__(
        self,
        pipeline: Pipeline,
        actions: frozenset[int],
        tasks: set[asyncio.Task[None]],
        fail: Callable[[core.PreparedRequest], None],
        source: Callable[[core.PreparedRequest], BodySource],
    ) -> None:
        self.pipeline = pipeline
        self.actions = actions
        self.with_data = frozenset(
            action
            for action in actions
            if needs_request_data(pipeline.bindings[action])
        )
        self.source = source
        self.tasks = tasks
        self.fail = fail
        self.runs: set[Run] = set()
        self.unwinding: set[asyncio.Task[None]] = set()
        self.loop = asyncio.get_running_loop()
        self.thread = threading.get_ident()

    def start(self, request: core.PreparedRequest) -> bool:
        """Drive the request's handler; ``False`` leaves it to the asyncio path."""
        action = request.action
        if (
            action is None
            or action not in self.actions
            or not request.native_handler
            or request.native_error is not None
        ):
            return False
        if request.id_ignored:
            log.warning("ignored invalid request ID from trusted proxy")
        binding = self.pipeline.bindings[action]
        run = Run(request, binding, self.pipeline.native_context(request.request_id))
        timeout = self.pipeline.app.runtime.request_timeout
        deadline = None if timeout is None else max(1, int(timeout * 1000))
        coro = self.pipeline.drive_native(
            binding, request.native_args, self._request_data(request, action)
        )
        self.runs.add(run)
        try:
            run.handle = core.run_native(
                coro,
                lambda value, error, reason: self._done(run, value, error, reason),
                lambda *handover: self._foreign(run, *handover),
                run.ctx,
                deadline,
                self.loop.call_soon_threadsafe,
            )
        except BaseException:
            self.runs.discard(run)
            raise
        return True

    def _request_data(
        self, request: core.PreparedRequest, action: int
    ) -> NativeBody | None:
        """What the guards and the body read beyond the prepared inputs."""
        if action not in self.with_data:
            return None
        return NativeBody(
            self.source(request),
            request.headers,
            request.query,
            request.client,
            request.native_principal,
        )

    def cancel_all(self) -> None:
        """Cancel every driven handler, as shutdown cancels the request tasks."""
        for run in list(self.runs):
            if run.handle is not None:
                run.handle.cancel()

    async def drain(self) -> None:
        """Wait for cancelled handlers to unwind, as ``gather`` waits for the
        cancelled request tasks."""
        while self.runs:
            await asyncio.sleep(0.005)

    def _done(
        self, run: Run, value: Any, error: BaseException | None, reason: str | None
    ) -> None:
        if threading.get_ident() != self.thread:
            self.loop.call_soon_threadsafe(self._finish, run, value, error, reason)
        else:
            self._finish(run, value, error, reason)

    def _finish(
        self, run: Run, value: Any, error: BaseException | None, reason: str | None
    ) -> None:
        self.runs.discard(run)
        if error is None:
            if isinstance(value, Response):
                self._respond(run, value)
            else:
                log.error("native handler of %s returned no response", run.rid)
                self.fail(run.request)
        elif isinstance(error, asyncio.CancelledError):
            if reason == "deadline":
                self._respond(run, run.ctx.run(timeout_response))
            # Cancelled at shutdown: dropped unanswered, as a cancelled task is.
        elif isinstance(error, Exception):
            self._translate_now(run, error)
        else:
            log.error("native handler of %s ended with %r", run.rid, error)
            self.fail(run.request)

    def _foreign(self, run: Run, *handover: Any) -> None:
        if threading.get_ident() != self.thread:
            self.loop.call_soon_threadsafe(self._foreign, run, *handover)
            return
        coro, ctx, yielded, remaining, reason = handover
        self.runs.discard(run)
        work = self.pipeline.native_continue(
            run.rid, resume(coro, yielded), remaining, reason
        )
        task = self._track(self._answer(run, work), ctx)
        if reason == "cancelled":
            self.unwinding.add(task)
            task.add_done_callback(self.unwinding.discard)

    async def _answer(self, run: Run, work: Awaitable[Response]) -> None:
        try:
            response = await work
        except Exception:
            self._broken(run)
            return
        self._respond(run, response)

    def _translate_now(self, run: Run, error: Exception) -> None:
        """Answer in place when no application error handler can match, else
        translate in a task."""
        try:
            response = run.ctx.run(self.pipeline.native_failure_now, error, run.rid)
        except Exception:
            self._broken(run)
            return
        if response is None:
            self._track(self._translate(run, error), run.ctx)
        else:
            self._respond(run, response)

    async def _translate(self, run: Run, error: Exception) -> None:
        await self._answer(run, self.pipeline.native_failure(error, run.rid))

    def _broken(self, run: Run) -> None:
        log.exception("request %s could not be translated", run.rid)
        self.fail(run.request)

    def _track(self, coro: Any, ctx: contextvars.Context) -> asyncio.Task[None]:
        task: asyncio.Task[None] = self.loop.create_task(coro, context=ctx)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def _respond(self, run: Run, response: Response) -> None:
        try:
            run.request.respond_final(response.status, response.headers, response.body)
        except Exception:
            log.exception("request %s could not be answered", run.rid)
            self.fail(run.request)
