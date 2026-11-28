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
from hinoki.auth import Caller, _LazyHeaders
from hinoki.db import NATIVE_OWNER, current, current_owner
from hinoki.events import AuthRejected, RateLimited
from hinoki.http.response import Response as HandlerResponse
from hinoki.orm.projection import _ProjectedItem, _ProjectedList, _ProjectedPage
from hinoki.runtime.binding import BODY_METHODS, Binding, parse_query
from hinoki.runtime.nativeproof import native_refusal
from hinoki.runtime.pipeline import NativeBody, enforce_response, shape_result
from hinoki.runtime.requests import authorize
from hinoki.runtime.responses import (
    BODYLESS,
    NO_PAYLOAD,
    HTTPError,
    Response,
)
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


def is_direct_route(binding: Binding) -> bool:
    """Whether ``NativeLane`` can invoke ``binding.route.handler`` directly,
    bypassing the multi-coroutine ``drive_native`` wrapper."""
    route = binding.route
    return (
        not route.atomic
        and binding.default_json
        and route.owner is None
        and not binding.resource_params
        and not binding.takes_request
    )


def is_auth_direct_route(binding: Binding) -> bool:
    """Whether an access-guarded route can run through the lightweight single-
    coroutine auth fast-path instead of ``drive_native``."""
    route = binding.route
    return (
        route.access is not None
        and route.rate_limit is None
        and binding.body_param is None
        and binding.query_param is None
        and is_direct_route(binding)
    )


async def _run_auth_direct(request: core.PreparedRequest, binding: Binding) -> Any:
    """Authenticate via ``binding.route.access`` and invoke the handler in a
    single coroutine frame without ``NativeBody`` or ``Response`` wrappers."""
    route = binding.route
    access = route.access
    if access is None:  # pragma: no cover - gated by is_auth_direct_route
        raise RuntimeError("auth direct route without access policy")
    native_principal = request.native_principal
    if native_principal is not None:
        subject, scopes, claims = native_principal
        principal: Any = Caller(subject, None, frozenset(scopes), claims=claims)
    else:
        raw_query = request.query
        query = parse_query(raw_query) if raw_query else {}
        principal = await authorize(
            access,
            _LazyHeaders(request),
            query,
            route=route.path,
            method=route.method,
        )
    handler: Callable[..., Any] = route.handler
    name = binding.principal_name
    if request.has_native_args or binding.native_defaults:
        supplied = request.native_args
        args = (
            binding.complete_native_args(supplied, (name, principal))
            if name is not None
            else binding.complete_native_args(supplied)
        )
        return await handler(**args)
    if name is not None:
        return await handler(**{name: principal})
    return await handler()


def _projected_fast_handle(binding: Binding, value: object) -> object | None:
    """Return the pre-encoded Rust handle when ``value`` is a lazy projection
    matching ``binding``'s declared response schema, bypassing check+narrow."""
    kind = type(value)
    if kind is _ProjectedList and isinstance(value, _ProjectedList):
        if binding.response_scan is not None and binding.narrow_schema is value._schema:
            return value._projected_handle
    elif kind is _ProjectedItem and isinstance(value, _ProjectedItem):
        schema = binding.response_schema or binding.narrow_schema
        if schema is value._schema and (
            binding.response_check is None or binding.response_check(value)
        ):
            return value._projected_handle
    elif (
        kind is _ProjectedPage
        and isinstance(value, _ProjectedPage)
        and binding.narrow_schema is value._schema
        and (binding.response_check is None or binding.response_check(value))
    ):
        return value._projected_handle
    return None


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
        self.direct: dict[int, Binding] = {
            action: pipeline.bindings[action]
            for action in actions
            if action not in self.with_data
            and is_direct_route(pipeline.bindings[action])
        }
        self.auth_direct: dict[int, Binding] = {
            action: pipeline.bindings[action]
            for action in actions
            if action in self.with_data
            and is_auth_direct_route(pipeline.bindings[action])
        }
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
        direct = self.direct.get(action)
        if direct is not None:
            self._start_direct(request, direct)
            return True
        auth_direct = self.auth_direct.get(action)
        if auth_direct is not None:
            self._start_direct_coro(
                request, auth_direct, _run_auth_direct(request, auth_direct)
            )
            return True
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

    def _start_direct(self, request: core.PreparedRequest, binding: Binding) -> None:
        """Invoke a plain unguarded JSON handler directly and poll its first step
        eagerly in Rust without allocating ``Run`` or wrapper coroutines."""
        handler: Callable[..., Any] = binding.route.handler
        try:
            if request.has_native_args or binding.native_defaults:
                coro = handler(**binding.complete_native_args(request.native_args))
            else:
                coro = handler()
        except Exception as error:
            rid = request.request_id
            self._translate_now(
                Run(request, binding, self.pipeline.native_context(rid), rid), error
            )
            return
        self._start_direct_coro(request, binding, coro)

    def _start_direct_coro(
        self,
        request: core.PreparedRequest,
        binding: Binding,
        coro: Any,
    ) -> None:
        """Poll ``coro`` eagerly in Rust; only allocate ``Run`` if it suspends."""
        rid = request.request_id
        ctx = self.pipeline.native_context(rid)
        try:
            kind, payload = core.step_native_first(coro, ctx)
        except Exception as error:
            self._translate_now(Run(request, binding, ctx, rid), error)
            return
        if kind == 0:
            self._send_direct(request, binding, ctx, rid, payload)
            return
        run = Run(request, binding, ctx, rid)
        if kind == 1:
            self.runs.add(run)
            try:
                run.handle = core.continue_native(
                    coro,
                    payload,
                    lambda value, error, reason: self._done_direct(
                        run, value, error, reason
                    ),
                    lambda *handover: self._foreign_direct(run, *handover),
                    ctx,
                    self.deadline,
                    self.steps,
                )
            except BaseException:
                self.runs.discard(run)
                raise
            return
        remaining = None if self.deadline is None else self.deadline / 1000.0
        self._foreign_direct(run, coro, ctx, payload, remaining, None)

    def _send_direct(
        self,
        request: core.PreparedRequest,
        binding: Binding,
        ctx: contextvars.Context,
        rid: str,
        value: Any,
    ) -> None:
        status = binding.route.status
        if status not in BODYLESS:
            handle = _projected_fast_handle(binding, value)
            if handle is not None:
                request.respond_final_json(status, None, handle)
                return
            if binding.fused_check and type(value) is list:
                try:
                    request.respond_final_json(status, None, value, True)
                    return
                except Exception:  # noqa: S110 - fall back to checked path below
                    pass
                self._send_direct_checked(request, binding, ctx, rid, value, True)
                return
        self._send_direct_checked(request, binding, ctx, rid, value, False)

    def _send_direct_checked(
        self,
        request: core.PreparedRequest,
        binding: Binding,
        ctx: contextvars.Context,
        rid: str,
        value: Any,
        disable_fused: bool,
    ) -> None:
        if disable_fused:
            object.__setattr__(binding, "fused_check", False)
        try:
            if isinstance(value, HandlerResponse):
                request.respond_final(value.status, value.headers, value.body)
                return
            exact = enforce_response(binding, value)
            status = binding.route.status
            if value is not None and status not in BODYLESS:
                shaped = shape_result(binding, value, exact)
                request.respond_final_json(status, None, shaped)
                return
            out_status = HTTPStatus.NO_CONTENT if status == HTTPStatus.OK else status
            request.respond_final(out_status, [], b"")
        except Exception as error:
            self._translate_now(Run(request, binding, ctx, rid), error)

    def _done_direct(
        self, run: Run, value: Any, error: BaseException | None, reason: str | None
    ) -> None:
        if threading.get_ident() != self.thread:
            self.loop.call_soon_threadsafe(
                self._finish_direct, run, value, error, reason
            )
        else:
            self._finish_direct(run, value, error, reason)

    def _finish_direct(
        self, run: Run, value: Any, error: BaseException | None, reason: str | None
    ) -> None:
        self.runs.discard(run)
        if error is None:
            self._send_direct(run.request, run.binding, run.ctx, run.rid, value)
        elif isinstance(error, asyncio.CancelledError):
            if reason == "deadline":
                self._respond(run, run.ctx.run(timeout_response))
        elif isinstance(error, Exception):
            self._translate_now(run, error)
        else:
            log.error("native handler of %s ended with %r", run.rid, error)
            self.fail(run.request)

    def _foreign_direct(self, run: Run, *handover: Any) -> None:
        if threading.get_ident() != self.thread:
            self.loop.call_soon_threadsafe(self._foreign_direct, run, *handover)
            return
        coro, ctx, yielded, remaining, reason = handover
        self.runs.discard(run)
        work = self.pipeline.native_continue(
            run.rid,
            self._wrap_direct_foreign(run.binding, resume(coro, yielded)),
            remaining,
            reason,
        )
        task = self._track(self._answer(run, work), ctx)
        if reason == "cancelled":
            self.unwinding.add(task)
            task.add_done_callback(self.unwinding.discard)

    async def _wrap_direct_foreign(
        self, binding: Binding, resumed: Awaitable[Any]
    ) -> Response:
        result = await resumed
        if isinstance(result, HandlerResponse):
            return await self.pipeline._plain_response(binding, result)
        fused = binding.fused_check and type(result) is list
        exact = False if fused else enforce_response(binding, result)
        route = binding.route
        if result is not None and route.status not in BODYLESS:
            shaped = shape_result(binding, result, exact)
            return self.pipeline._native_rendered(
                binding, shaped, None, defer=True, strict=fused
            )
        if route.status == HTTPStatus.OK:
            return Response(HTTPStatus.NO_CONTENT)
        return Response(route.status)

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
            if threading.get_ident() != self.thread:
                self.loop.call_soon_threadsafe(self._track_translate, run, error)
            else:
                self._track(self._translate(run, error), run.ctx)
        else:
            self._respond(run, response)

    def _track_translate(self, run: Run, error: Exception) -> None:
        self._track(self._translate(run, error), run.ctx)

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
