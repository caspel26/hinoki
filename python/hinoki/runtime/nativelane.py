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

from hinoki import context, core
from hinoki.db import NATIVE_OWNER, current, current_owner
from hinoki.events import AuthRejected, RateLimited
from hinoki.runtime.binding import BODY_METHODS, Binding
from hinoki.runtime.nativeproof import native_refusal
from hinoki.runtime.pipeline import NativeBody, enforce_response
from hinoki.runtime.responses import NO_PAYLOAD, HTTPError, Response
from hinoki.runtime.servercore import native_handler_eligible, route_guarded

if TYPE_CHECKING:
    import contextvars

    from hinoki.events import EventBus
    from hinoki.runtime.pipeline import BodySource, Pipeline

log = logging.getLogger("hinoki.runtime")


def build_native_body(
    source: BodySource | None, binding: Binding, request: core.PreparedRequest
) -> NativeBody:
    """Build ``NativeBody`` without materializing raw headers or header dict
    unless the route actually needs them."""
    principal = request.native_principal
    route = binding.route
    needs_raw = route.rate_limit is not None
    needs_dict = (
        binding.body_param is not None
        or binding.query_param is not None
        or len(route.renderers) > 1
    )
    needs_auth = route.access is not None and principal is None
    needs_query = binding.query_param is not None or needs_auth
    return NativeBody(
        source,
        request.headers if needs_raw else [],
        request.query if needs_query else b"",
        request.client if needs_raw else None,
        principal,
        request.header_dict if needs_dict else None,
        request if (not needs_dict and needs_auth) else None,
    )


def needs_request_data(binding: Binding) -> bool:
    """Whether the route reads the headers, the peer or the body itself."""
    return binding.route.method in BODY_METHODS or route_guarded(binding)


def lane_refusal(binding: Binding, bus: EventBus | None = None) -> str | None:
    """Why the native lane does not serve this route, or ``None`` when it does.
    ``bus`` is the application's: a write with a handler for its model event
    stays on the Python path, where the handler can run."""
    route = binding.route
    reason = None
    if binding.streams:
        reason = "streamed response"
    elif not binding.is_async:
        reason = "not an async def handler"
    elif not native_handler_eligible(binding):
        reason = "not natively eligible"
    return (
        reason
        or guard_event_refusal(binding, bus)
        or native_refusal(route.handler, bus)
    )


def guard_event_refusal(binding: Binding, bus: EventBus | None) -> str | None:
    """A guard refusal is told to its event handlers from an asyncio task,
    which the lane does not have: such a route stays on the Python path."""
    route = binding.route
    if bus is None:
        return None
    if route.access is not None and bus.registered(AuthRejected, None):
        return "an AuthRejected handler is registered"
    if route.rate_limit is not None and bus.registered(RateLimited, None):
        return "a RateLimited handler is registered"
    return None


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
        rid: str | None = None,
    ) -> None:
        self.request = request
        self.binding = binding
        self.ctx = ctx
        self.rid = request.request_id if rid is None else rid
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
        self.steps = core._NativeSteps(self.loop)
        self.thread = threading.get_ident()
        timeout = pipeline.app.runtime.request_timeout
        self.deadline = None if timeout is None else max(1, int(timeout * 1000))

    def start(self, request: core.PreparedRequest) -> bool:
        """Drive the request's handler; ``False`` leaves it to the asyncio path."""
        action = request.action
        if (
            action not in self.actions
            or not request.native_handler
            or request.native_error is not None
        ):
            return False
        if request.id_ignored:
            log.warning("ignored invalid request ID from trusted proxy")
        binding = self.pipeline.bindings[action]
        rid = request.request_id
        run = Run(request, binding, self.pipeline.native_context(rid), rid)
        coro = self.pipeline.drive_native(
            binding, request.native_args, self._request_data(request, action, binding)
        )
        self.runs.add(run)
        try:
            run.handle = core.run_native(
                coro,
                lambda value, error, reason: self._done(run, value, error, reason),
                lambda *handover: self._foreign(run, *handover),
                run.ctx,
                self.deadline,
                self.steps,
            )
        except BaseException:
            self.runs.discard(run)
            raise
        return True

    def _request_data(
        self, request: core.PreparedRequest, action: int, binding: Binding
    ) -> NativeBody | None:
        """What the guards and the body read beyond the prepared inputs."""
        if action not in self.with_data:
            return None
        source = self.source(request) if binding.body_param is not None else None
        return build_native_body(source, binding, request)

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
        error = self._send(run, response)
        if error is not None:
            await self._answer(run, self.pipeline.native_failure(error, run.rid))

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

    def _send(self, run: Run, response: Response) -> Exception | None:
        headers = response.headers
        extra = run.ctx.get(context.rate_limit_headers, ())
        if extra:
            headers = [*headers, *((n.encode(), v.encode()) for n, v in extra)]
        if response.payload is not NO_PAYLOAD:
            try:
                if response.strict:
                    self._send_strict(run, response.status, headers, response.payload)
                else:
                    run.request.respond_final_json(
                        response.status, headers, response.payload
                    )
            except Exception as error:
                return error
            return None
        try:
            run.request.respond_final(response.status, headers, response.body)
        except Exception:
            run.ctx.run(log.exception, "request %s could not be answered", run.rid)
            self.fail(run.request)
        return None

    @staticmethod
    def _send_strict(
        run: Run, status: int, headers: list[tuple[bytes, bytes]], payload: object
    ) -> None:
        """Check and encode in one pass; any miss or error sends nothing and
        runs the checked path, so its contract error and encode errors stand.
        The first miss turns the fused path off for the binding: rows the strict
        mode cannot vouch for would otherwise pay it on every request."""
        try:
            run.request.respond_final_json(status, headers, payload, True)
            return
        except Exception:  # noqa: S110 - the checked path below answers it
            pass
        # Before the check, so an error raised there still disables it.
        object.__setattr__(run.binding, "fused_check", False)
        enforce_response(run.binding, payload)
        run.request.respond_final_json(status, headers, payload)

    def _respond(self, run: Run, response: Response) -> None:
        error = self._send(run, response)
        if error is not None:
            self._translate_now(run, error)
