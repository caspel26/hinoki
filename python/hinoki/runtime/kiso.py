"""One worker of the Rust HTTP server (HTTP/1.1, and HTTP/2 over TLS).

The listener, the HTTP parsing and the connection limits run on the native
tokio runtime (``core.KisoServer``). Every request the native classifier does
not answer waits in a bounded queue; this module drains it on the asyncio loop,
runs ``Pipeline.handle`` for each one and hands the answer back to the
connection. The handlers and the pipeline run in Python, on the loop.

A request body stays in the native connection until the pipeline asks for it
(after authentication and negotiation, as on the ASGI path); it is read by the
native layer with the same limits and error codes. ``server.trusted_proxies``
applies as on the ASGI path: the native layer matches the TCP peer against it
(for the request id and the policy headers of its own answers) and the pipeline
resolves the forwarded scheme and client of queued requests from the same peer.

``hinoki serve --server kiso`` runs one worker in the CLI process, or, for
several workers (and always under ``hinoki dev``), starts this module as a
process per worker from ``hinoki.runtime.supervisor``, which owns the listening
socket and passes it down. Run by hand it binds itself:

    python -m hinoki.runtime.kiso module:app --port 8000
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from http import HTTPStatus
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from hinoki import __version__, context, core
from hinoki.db import Database
from hinoki.errors import InvalidBody
from hinoki.http.stream import EventStream, Sourced
from hinoki.mail import Mailer
from hinoki.runtime import banner
from hinoki.runtime.listener import inherited_address, open_listener, signal_ready
from hinoki.runtime.nativelane import (
    NativeLane,
    build_native_body,
    lane_refusal,
    needs_request_data,
)
from hinoki.runtime.pipeline import enforce_response
from hinoki.runtime.recent import RequestRecorder
from hinoki.runtime.responses import NO_PAYLOAD, HTTPError, Response, StreamHandle
from hinoki.runtime.servercore import (
    build_server_core,
    claims_route_access,
    route_auth_stale,
    route_auth_state,
)
from hinoki.runtime.streaming import SourceReader, keepalive_of, pump_sink
from hinoki.serve import load_target, settings_of
from hinoki.settings import RUST_ONLY, AppSettings, ServerSettings

if TYPE_CHECKING:
    from hinoki.app import Hinoki

log = logging.getLogger("hinoki.runtime")
access_logger = logging.getLogger("hinoki.access")


def _rate_limit_pairs() -> list[tuple[bytes, bytes]]:
    """The ``RateLimit-*`` headers of the admitted request, as raw header pairs."""
    return [
        (name.encode(), value.encode())
        for name, value in context.rate_limit_headers.get()
    ]


def _with_rate_limit(
    route_headers: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    extra = context.rate_limit_headers.get()
    return route_headers + extra if extra else route_headers


def log_access(batch: Sequence[str]) -> None:
    """Hand Kiso's JSON access records to the ``hinoki.access`` logger (called
    from Kiso's logging thread, a batch at a time). Each record carries its
    fields as ``record.access`` for handlers and formatters that want them."""
    for text in batch:
        fields = core.decode_json(text.encode())
        access_logger.info(
            "%s %s %s %s %.1fms",
            fields["peer"],
            fields["method"],
            fields["path"],
            fields["status"],
            fields["duration_ms"],
            extra={"access": fields},
        )


# Requests taken from the native queue per call; a short batch means it is empty.
DRAIN_BATCH = 256

# Seconds between housekeeping passes (native counters, supervisor liveness).
HOUSEKEEPING = 1.0


class RustBody:
    """The body source of a queued request: the native layer reads it, and
    its failures become the errors the ASGI path raises."""

    __slots__ = ("request",)

    def __init__(self, request: core.PreparedRequest) -> None:
        self.request = request

    async def read(self, limit: int) -> bytes:
        try:
            return await self.request.body(limit)
        except core.BodyReadError as exc:
            raise body_failure(exc.args[0]) from None


def body_failure(kind: str) -> Exception:
    """The error for a native body failure: ``too_large``, ``timeout``,
    ``malformed`` (broken framing) or ``disconnected``. The last two share the
    ASGI path's ``disconnected`` code and status; only the message differs."""
    if kind == "too_large":
        return HTTPError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "payload_too_large")
    if kind == "timeout":
        return HTTPError(HTTPStatus.REQUEST_TIMEOUT, "request_timeout")
    if kind == "malformed":
        return InvalidBody("disconnected", "the request body is malformed")
    return InvalidBody("disconnected", "the client disconnected")


async def _drain_native_uninterruptibly(
    server_core: core.ServerCore, timeout_ms: int
) -> tuple[int, bool]:
    """Finish the tracked Rust drain before allowing pool detachment."""
    drain_task = asyncio.ensure_future(server_core._drain_native(timeout_ms))
    interrupted = False
    while not drain_task.done():
        try:
            await asyncio.shield(drain_task)
        # The drain must finish; the interruption is reported to the caller.
        except asyncio.CancelledError:  # NOSONAR
            interrupted = True
    return drain_task.result(), interrupted


def scope_of(
    request: core.PreparedRequest, server: tuple[str, int], scheme: str = "http"
) -> dict[str, Any]:
    """The ASGI-shaped scope the pipeline reads, from a queued request.
    ``server`` is where the worker listens; ``Request.host`` falls back to it
    for an HTTP/1.0 request without a ``Host`` header. ``scheme`` is ``https``
    when the worker terminates TLS itself; ``http_version`` is ``2`` for a
    request that arrived over HTTP/2."""
    return {
        "type": "http",
        "http_version": request.http_version,
        "method": request.method,
        "scheme": scheme,
        "path": unquote(request.path),
        # Non-ASCII bytes stay: the pipeline answers 400 bad_path_encoding for them.
        "raw_path": request.path.encode("utf-8"),
        "query_string": request.query,
        "headers": request.headers,
        "client": request.client,
        "server": server,
    }


class KisoWorker:
    """Runs one application on the Rust server inside the current event loop."""

    # The routes Rust authenticates, with what it copied; set at startup.
    _route_auth: Mapping[int, tuple[Any, tuple[object, ...]]] = MappingProxyType({})
    reuse_port: bool = False

    def __init__(  # noqa: PLR0913 - one parameter per native limit
        self,
        app: Hinoki,
        host: str,
        port: int,
        *,
        max_connections: int = 10_000,
        header_read_timeout: float = 10.0,
        body_read_timeout: float = 10.0,
        write_timeout: float = 30.0,
        tls_cert_file: str | None = None,
        tls_key_file: str | None = None,
        http2: bool = False,
        backlog: int = 1024,
        listener_fd: int | None = None,
        reuse_port: bool = False,
        parent: int | None = None,
        native_dispatch: bool = True,
        access_log: str | None = None,
        access_log_skip: Sequence[str] | None = None,
        access_log_slow_ms: float | None = None,
        access_log_sink: Callable[[Sequence[str]], None] | None = None,
        server_timing: bool = False,
        compression: dict[str, object] | None = None,
        metrics: dict[str, object] | None = None,
        ready_fd: int | None = None,
    ) -> None:
        self.adapter = app.asgi()
        self.pipeline = self.adapter.pipeline
        self.host = host
        self.port = port
        self.max_connections = max_connections
        self.header_read_timeout = header_read_timeout
        self.body_read_timeout = body_read_timeout
        self.write_timeout = write_timeout
        self.tls_cert_file = tls_cert_file
        self.tls_key_file = tls_key_file
        self.http2 = http2
        self.backlog = backlog
        self.native_dispatch = native_dispatch
        self.access_log = access_log
        self.access_log_skip = access_log_skip
        self.access_log_slow_ms = access_log_slow_ms
        self.access_log_sink = access_log_sink
        self.server_timing = server_timing
        self.compression = compression
        self.metrics = (
            None
            if metrics is None
            else {**metrics, "events": app.events.metric_series()}
        )
        self.lane: NativeLane | None = None
        # An inherited listening descriptor (a supervisor owns the socket); the
        # native server takes it over. Without one, the worker binds itself.
        self.listener_fd = listener_fd
        self.reuse_port = reuse_port
        # The pid of the supervisor that started this worker, if any: the worker
        # stops when its parent is no longer that process.
        self.parent = parent
        # Where a supervisor that replaces workers waits for this one to serve.
        self.ready_fd = ready_fd
        self.timer: asyncio.TimerHandle | None = None
        # Starts not ready; the lifespan startup flips it.
        self.core = build_server_core(self.pipeline)
        # Taken with the core, before the startup hooks run: what Rust copied.
        self._route_auth = {
            action: (access, route_auth_state(access))
            for action, binding in enumerate(self.pipeline.bindings)
            if (access := claims_route_access(binding, self.pipeline.events))
            is not None
        }
        self.server: core.KisoServer | None = None
        self.address = (host, port)
        self.scheme = "https" if tls_cert_file else "http"
        self.tasks: set[asyncio.Task[None]] = set()
        self.stopping = asyncio.Event()
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._lifespan_task: asyncio.Task[None] | None = None
        self._disarmed_crud_actions: set[int] = set()

    async def _lifespan(self, event: str) -> dict[str, Any]:
        """Send one lifespan event through the ASGI adapter and wait for its answer."""
        if self._lifespan_task is None:
            self._lifespan_task = asyncio.create_task(
                self.adapter({"type": "lifespan"}, self._inbox.get, self._outbox.put)
            )
        await self._inbox.put({"type": f"lifespan.{event}"})
        return await self._outbox.get()

    async def _finish_lifespan(self) -> None:
        task, self._lifespan_task = self._lifespan_task, None
        if task is not None:
            await task

    async def start(self) -> int:
        """Run the lifespan startup, then bind. Returns the bound port. Nothing
        is bound when startup fails."""
        answer = await self._lifespan("startup")
        if answer["type"] != "lifespan.startup.complete":
            await self._finish_lifespan()
            raise RuntimeError(f"startup failed: {answer.get('message', '')}")
        runtime = self.pipeline.app.runtime
        try:
            self._activate_crud_plans()
            self.lane = self._select_lane()
            if self.listener_fd is None:
                # Bound only now, after the startup succeeded: a failed startup
                # leaves nothing listening.
                listener = open_listener(
                    self.host, self.port, self.backlog, reuse_port=self.reuse_port
                )
                self.listener_fd = listener.detach()
            fd, self.listener_fd = self.listener_fd, None  # taken, even on failure
            server = core.KisoServer.start(
                self.core,
                self.host,
                self.port,
                self.max_connections,
                self.header_read_timeout,
                runtime.shutdown_timeout,
                runtime.max_header_bytes,
                # Below the request timeout, or a stalled body would be a 504.
                min(self.body_read_timeout, runtime.request_timeout / 2),
                fd,
                self.write_timeout,
                self.tls_cert_file,
                self.tls_key_file,
                self.http2,
                self.access_log,
                self.access_log_skip,
                self.access_log_slow_ms,
                self.access_log_sink,
                self.server_timing,
                self.compression,
                self._metrics_with_email(),
            )
        except BaseException:
            self.core._disarm_crud_plans()
            await _drain_native_uninterruptibly(
                self.core, int(runtime.shutdown_timeout * 1000)
            )
            self.core._detach_database()
            await self._lifespan("shutdown")
            await self._finish_lifespan()
            raise
        self.server = server
        if self.metrics is not None:
            self.pipeline.events.attach_metrics(server.event_metrics())
            self.pipeline.attach_rate_limit_metrics(server.rate_limit_metrics())
            self._attach_mail_metrics(server)
        self.address = (self.host, server.port)
        loop = asyncio.get_running_loop()
        loop.add_reader(server.wake_fd, self._wake, server)
        self.timer = loop.call_later(HOUSEKEEPING, self._tick)
        self._check_route_auth()
        self.core.set_ready(True)
        return server.port

    def _metrics_with_email(self) -> dict[str, object] | None:
        """The metrics settings, with the mailer's backend once the lifespan
        has opened it (the series are fixed when the server starts)."""
        if self.metrics is None:
            return None
        mailer = self.pipeline.ctx.find(Mailer)
        settings: dict[str, object] = dict(self.metrics)
        if mailer is not None:
            settings["email"] = mailer.backend.value
        if self.pipeline.uses_rate_limit():
            settings["rate_limits"] = True
        return settings

    def _attach_mail_metrics(self, server: core.KisoServer) -> None:
        mailer = self.pipeline.ctx.find(Mailer)
        recorder = server.mail_metrics()
        if mailer is not None and recorder is not None:
            mailer.attach_metrics(recorder)

    def _select_lane(self) -> NativeLane | None:
        """The native lane for the routes that prove eligible, once at startup."""
        if not self.native_dispatch:
            return None
        actions = set()
        for action, binding in enumerate(self.pipeline.bindings):
            refusal = lane_refusal(binding, self.pipeline.events)
            if refusal is None:
                actions.add(action)
            log.debug(
                "%s %s: %s",
                binding.route.method,
                binding.route.path,
                "native lane" if refusal is None else f"asyncio ({refusal})",
            )
        if not actions:
            return None
        return NativeLane(
            self.pipeline, frozenset(actions), self.tasks, self._fail, RustBody
        )

    def _activate_crud_plans(self) -> None:
        """Attach the worker-owned pool clone and arm still-valid plans."""
        selections = self.pipeline.crud_plan_selections
        if not any(selection is not None for selection in selections):
            return
        self._check_crud_snapshots()
        if not any(
            selection is not None and action not in self._disarmed_crud_actions
            for action, selection in enumerate(selections)
        ):
            return
        database = self.pipeline.ctx.find(Database)
        if type(database) is not Database:
            self._disarm_crud_plans("database_missing_after_startup")
            return
        if not self.core._database_attached():
            try:
                self.core._attach_database(database.engine)
            except Exception:
                log.exception("could not attach the native CRUD database")
                self._disarm_crud_plans("database_attach_failed")
                return
        for action, selection in enumerate(selections):
            if (
                selection is not None
                and action not in self._disarmed_crud_actions
                and not self.core._arm_crud(action)
            ):
                self.core._disarm_crud(action)
                self._disarmed_crud_actions.add(action)
                self.pipeline.crud_plan_reasons[action] = "native_plan_arm_failed"
                log.warning("native CRUD plan %s could not be armed", action)

    def _disarm_crud_plans(self, reason: str) -> None:
        for action, selection in enumerate(self.pipeline.crud_plan_selections):
            if selection is None or action in self._disarmed_crud_actions:
                continue
            self.core._disarm_crud(action)
            self._disarmed_crud_actions.add(action)
            self.pipeline.crud_plan_reasons[action] = reason
            log.warning("native CRUD plan %s disarmed: %s", action, reason)

    def _check_crud_snapshots(self) -> None:
        for action, selection in enumerate(self.pipeline.crud_plan_selections):
            if selection is None or action in self._disarmed_crud_actions:
                continue
            try:
                valid = selection.snapshot_valid()
            except Exception:
                valid = False
            if valid:
                continue
            self.core._disarm_crud(action)
            self._disarmed_crud_actions.add(action)
            self.pipeline.crud_plan_reasons[action] = "native_plan_stale"
            log.warning("native CRUD plan %s disarmed after staleness check", action)

    def _check_route_auth(self) -> None:
        """Send a route back to Python once its access policy or provider no
        longer is what Rust holds (a key, issuer, audience, leeway, scope claim,
        permission or challenge changed)."""
        armed = {}
        for action, held in self._route_auth.items():
            if self._route_auth_stale(action, held):
                self.core._disarm_route_auth(action)
                log.warning("native authentication of route %s disarmed", action)
            else:
                armed[action] = held
        self._route_auth = armed

    def _route_auth_stale(
        self, action: int, held: tuple[Any, tuple[object, ...]]
    ) -> bool:
        try:
            return route_auth_stale(
                self.pipeline.bindings[action], *held, self.pipeline.events
            )
        except Exception:
            return True

    def _tick(self) -> None:
        """One pass, then the next: a failing pass must not end the chain (it
        also detects a dead supervisor)."""
        try:
            self.housekeeping()
        except Exception:
            log.exception("housekeeping failed")
        finally:
            self.timer = asyncio.get_running_loop().call_later(HOUSEKEEPING, self._tick)

    def housekeeping(self) -> None:
        """Once a second: report invalid trusted request ids that native
        answers ignored (queued requests log their own), and stop when the
        supervisor that started this worker has died."""
        ignored = self.core.take_ignored_ids()
        if ignored:
            log.warning(
                "ignored invalid request ID from trusted proxy (%d natively "
                "answered requests)",
                ignored,
            )
        self._check_crud_snapshots()
        self._check_route_auth()
        if self.parent is not None and os.getppid() != self.parent:
            log.error("the supervisor is gone; stopping")
            self.stopping.set()

    def _wake(self, server: core.KisoServer) -> None:
        """The loop's reader callback: clear the wake socket first, then drain
        the queue until it is empty (a short batch), as the queue requires. It
        keeps running while the server shuts down, so requests already queued
        still get their answer. The wake is edge-only, so a failed drain
        schedules itself again: queued requests are never left without a
        pending wake. An EOF on the wake socket means the server ended."""
        loop = asyncio.get_running_loop()
        if not server.clear_wake():
            loop.remove_reader(server.wake_fd)
            return
        try:
            self._drain()
        except Exception:
            log.exception("draining the request queue failed; retrying")
            loop.call_soon(self._wake, server)

    def _drain(self) -> None:
        core.handoff_batch(self._drain_loop)

    def _drain_loop(self) -> None:
        while True:
            batch = self.core.drain(DRAIN_BATCH)
            for request in batch:
                try:
                    self._spawn(request)
                except Exception:
                    log.exception("request %s could not be started", request.request_id)
                    self._fail(request)
            if len(batch) < DRAIN_BATCH:
                return

    def _spawn(self, request: core.PreparedRequest) -> None:
        if request.method == "WS":
            task = asyncio.create_task(self._serve_websocket(request))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return
        if self.lane is not None and self.lane.start(request):
            return
        failure = request._take_native_failure()
        if failure is not None and self._answer_failure(request, failure):
            return
        task = asyncio.create_task(
            self._serve(request)
            if failure is None
            else self._serve_failure(request, failure)
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _serve_websocket(self, request: core.PreparedRequest) -> None:
        """Judge the handshake like an HTTP action; a refusal is answered
        instead of the upgrade, an admitted one switches the connection and runs
        the handler until either side closes."""
        if request.id_ignored:
            log.warning("ignored invalid request ID from trusted proxy")
        rid = request.request_id
        try:
            action = request.action
            if action is None:
                raise RuntimeError("WebSocket request has no action")
            route = self.pipeline.websockets[action]
            admitted = await self.pipeline.admit_websocket(
                rid,
                route,
                scope_of(request, self.address, self.scheme),
                request.prepared_path_params,
            )
            if isinstance(admitted, Response):
                request.respond_routed(
                    admitted.status, admitted.headers, admitted.body, ()
                )
                return
            socket = request.accept_websocket(admitted.subprotocol)
            if socket is not None:
                await self.pipeline.run_websocket(
                    rid, route, socket, admitted, unquote(request.path)
                )
        except Exception:
            log.exception("request %s failed", rid)
            self._fail(request)

    async def _serve_failure(
        self, request: core.PreparedRequest, failure: core.NativeFailure
    ) -> None:
        """Translate a Rust CRUD failure an application error handler may
        need a coroutine for."""
        if request.id_ignored:
            log.warning("ignored invalid request ID from trusted proxy")
        try:
            response = await self._serve_native_failure(request, failure)
            request.respond_final(response.status, response.headers, response.body)
        except Exception:
            log.exception("request %s failed", request.request_id)
            self._fail(request)

    def _answer_failure(
        self, request: core.PreparedRequest, failure: core.NativeFailure
    ) -> bool:
        """Answer a Rust CRUD failure in place when no application error
        handler can match; ``False`` leaves it to a task."""
        action = request.action
        if action is None:
            return False
        try:
            response = self.pipeline.crud_failure_now(
                action, request.request_id, failure
            )
        except Exception:
            log.exception("request %s failed", request.request_id)
            self._fail(request)
            return True
        if response is None:
            return False
        if request.id_ignored:
            log.warning("ignored invalid request ID from trusted proxy")
        try:
            request.respond_final(response.status, response.headers, response.body)
        except Exception:
            log.exception("request %s could not be answered", request.request_id)
            self._fail(request)
        return True

    def _fail(self, request: core.PreparedRequest) -> None:
        """Answer a request the worker could not serve with the usual 500."""
        rid = request.request_id
        try:
            response = self.pipeline.with_headers(
                HTTPError(500, "internal_error", request_id=rid).response,
                scope_of(request, self.address, self.scheme),
                rid,
            )
            body = b"" if request.method == "HEAD" else response.body
            request.respond(response.status, response.headers, body)
        except Exception:
            log.exception("request %s could not be answered", rid)

    async def _serve_native_failure(
        self, request: core.PreparedRequest, failure: core.NativeFailure
    ) -> Response:
        action = request.action
        if action is None:
            raise RuntimeError("native failure request has no action or failure")
        return await self.pipeline.handle_native_failure(
            action, request.request_id, failure
        )

    async def _serve_native_rows(
        self, request: core.PreparedRequest, fallback: core.NativeRows
    ) -> Response:
        action = request.action
        if action is None:
            raise RuntimeError("native rows request has no action or rows")
        return await self.pipeline.handle_native_rows(
            action, request.request_id, fallback
        )

    async def _pipeline_response(
        self, request: core.PreparedRequest
    ) -> tuple[Response, bool]:
        if request.native_handler:
            action = request.action
            if action is None:
                raise RuntimeError("native handler request has no action")
            binding = self.pipeline.bindings[action]
            native_body = (
                build_native_body(
                    RustBody(request) if binding.body_param is not None else None,
                    binding,
                    request,
                )
                if needs_request_data(binding)
                else None
            )
            return await self.pipeline.handle_native(
                binding,
                request.request_id,
                request.native_args,
                request.native_error,
                native_body,
                True,
            ), True
        failure = request._take_native_failure()
        if failure is not None:
            return await self._serve_native_failure(request, failure), True
        fallback = request._take_native_rows()
        if fallback is not None:
            return await self._serve_native_rows(request, fallback), True
        route = (
            None
            if request.action is None
            else (request.action, request.prepared_path_params)
        )
        return await self.pipeline.handle_unfinished(
            scope_of(request, self.address, self.scheme),
            RustBody(request),
            request.request_id,
            route,
            vetted=True,
        ), False

    async def _serve(self, request: core.PreparedRequest) -> None:
        """Answer one queued request. A request whose task is cancelled (at
        shutdown, after ``shutdown_timeout``) is dropped unanswered: its
        connection is closed by the native layer without a response, so the
        client sees EOF, not a 503."""
        if request.id_ignored:
            log.warning("ignored invalid request ID from trusted proxy")
        try:
            response, native_final = await self._pipeline_response(request)
            if response.stream is not None:
                await self._send_stream(request, response, response.stream)
            elif native_final:
                await self._send_native_final(request, response)
            else:
                # Set by the pipeline in this task's context while it answered.
                request.respond_routed(
                    response.status,
                    response.headers,
                    response.body,
                    _with_rate_limit(context.route_headers.get()),
                )
        except Exception:
            log.exception("request %s failed", request.request_id)
            # The route's own headers may be what failed; the 500 must not reuse them.
            context.route_headers.set(())
            self._fail(request)

    async def _send_native_final(
        self, request: core.PreparedRequest, response: Response
    ) -> None:
        headers = [*response.headers, *_rate_limit_pairs()]
        payload = getattr(response, "payload", NO_PAYLOAD)
        if payload is not NO_PAYLOAD:
            try:
                if response.strict:
                    self._send_native_strict(request, response.status, headers, payload)
                else:
                    request.respond_final_json(response.status, headers, payload)
            except Exception as error:
                err = await self.pipeline.handle_native_error(error, request.request_id)
                request.respond_final(err.status, err.headers, err.body)
            return
        request.respond_final(response.status, headers, response.body)

    def _send_native_strict(
        self,
        request: core.PreparedRequest,
        status: int,
        headers: list[tuple[bytes, bytes]],
        payload: object,
    ) -> None:
        try:
            request.respond_final_json(status, headers, payload, True)
            return
        except Exception:  # noqa: S110 - the checked path below answers it
            pass
        action = request.action
        if action is not None:
            binding = self.pipeline.bindings[action]
            object.__setattr__(binding, "fused_check", False)
            enforce_response(binding, payload)
        request.respond_final_json(status, headers, payload)

    async def _send_stream(
        self, request: core.PreparedRequest, response: Response, handle: StreamHandle
    ) -> None:
        """Send a streamed answer: the head, then the body. The place the answer
        holds is given back when the body ends (a file is sent by the server alone, so
        its place is given back at once)."""
        route_headers = _with_rate_limit(context.route_headers.get())
        try:
            if handle.location is not None:
                request.respond_file(
                    response.status, response.headers, route_headers, handle.location[0]
                )
                return
            await self._send_sourced(request, response, handle, route_headers)
        finally:
            handle.release()

    async def _send_sourced(
        self,
        request: core.PreparedRequest,
        response: Response,
        handle: StreamHandle,
        route_headers: tuple[tuple[str, str], ...],
    ) -> None:
        answer = handle.answer
        if not isinstance(answer, Sourced):
            raise RuntimeError(
                "only a sourced answer reaches the sink pump"
            )  # pragma: no cover
        events = isinstance(answer, EventStream)
        sink = request.respond_stream(
            response.status,
            response.headers,
            route_headers,
            None if events else self.pipeline.app.runtime.idle_timeout,
            keepalive_of(answer, self.pipeline.app.runtime.sse_keepalive),
            answer.length,
        )
        reader = SourceReader(answer, self.pipeline.executor)
        if sink is None or request.method == "HEAD":
            if sink is not None:
                sink.finish()
            await reader.close()
            return
        await pump_sink(
            sink, reader, answer.preamble, self.pipeline.streams if events else None
        )

    async def stop(self) -> int:
        """Stop accepting, let running requests finish (up to the shutdown
        timeout), then cancel what is left and run the lifespan shutdown.
        Returns the number of connections that had to be aborted."""
        server, self.server = self.server, None
        if server is None:
            return 0
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        try:
            self.housekeeping()  # the last second's counts
        except Exception:
            log.exception("housekeeping failed")
        drain_interrupted = False
        self.pipeline.streams.stop_events()  # SSE streams end with a normal close
        try:
            # Not ready only once the connections are drained: a request that a
            # client already sent to this worker (a reload stops one while its
            # successor serves) is answered, not refused with a 503.
            aborted = await server.shutdown()
        finally:
            self.core.set_ready(False)
            asyncio.get_running_loop().remove_reader(server.wake_fd)
            try:
                native_aborted, drain_interrupted = await _drain_native_uninterruptibly(
                    self.core,
                    int(self.pipeline.app.runtime.shutdown_timeout * 1000),
                )
                if native_aborted:
                    log.warning(
                        "aborted %d native CRUD jobs during shutdown", native_aborted
                    )
            finally:
                self.core._disarm_crud_plans()
                self.core._detach_database()
            await self._cancel_requests()
            await self._lifespan("shutdown")
            await self._finish_lifespan()
        if drain_interrupted:
            raise asyncio.CancelledError
        return aborted

    async def _cancel_requests(self) -> None:
        unwinding: set[asyncio.Task[None]] = set()
        if self.lane is not None:
            self.lane.cancel_all()
            unwinding = self.lane.unwinding
        cancelled = self.tasks - unwinding
        for task in list(cancelled):
            task.cancel()
        if self.lane is not None:
            await self.lane.drain()
            # A step queued before shutdown can still start a translation task.
            for task in list(self.tasks - unwinding - cancelled):
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def _ignore_signal(self) -> None:
        """A supervised worker leaves SIGINT and SIGHUP to its supervisor: a
        terminal Ctrl-C or hang-up reaches the whole process group, and a
        second signal here would abort the graceful stop."""

    def _on_signal(self) -> None:
        if self.stopping.is_set():
            # A supervised worker also gets the supervisor's forwarded SIGTERM
            # after a group-wide one; the supervisor owns escalation.
            if self.parent is None:
                os._exit(1)  # a second signal does not wait
            return
        self.stopping.set()

    async def run(self) -> int:
        port = await self.start()
        try:
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGTERM, self._on_signal)
            interrupt = self._on_signal if self.parent is None else self._ignore_signal
            loop.add_signal_handler(signal.SIGINT, interrupt)
            loop.add_signal_handler(signal.SIGHUP, interrupt)
            if self.ready_fd is not None:
                # After the handlers: the supervisor's SIGTERM to a worker it
                # replaces comes right after this, and must stop it gracefully.
                signal_ready(self.ready_fd)
            if self.parent is None:
                shown = f"[{self.host}]" if ":" in self.host else self.host
                print(f"listening on {self.scheme}://{shown}:{port}", flush=True)
            await self.stopping.wait()
        finally:
            aborted = await self.stop()
        if aborted:
            log.warning("aborted %d connections at shutdown", aborted)
        return 0


def access_style(settings: ServerSettings, dev: bool) -> str | None:
    """The access-log style for a worker: none, the configured format, or by
    default the coloured development line on a colour terminal and a plain line
    otherwise."""
    if not settings.logs_access(dev):
        return "logger" if settings.recent_requests else None
    if settings.access_log_format is not None:
        return settings.access_log_format
    return "pretty" if banner.wants_colour() else "plain"


def access_sink(
    settings: ServerSettings, dev: bool
) -> Callable[[Sequence[str]], None] | None:
    """What receives Kiso's JSON records besides stderr: the ``hinoki.access``
    logger and the recent-requests file, as configured."""
    sinks: list[Callable[[Sequence[str]], None]] = []
    if settings.logs_access(dev) and settings.access_log_format == "logger":
        sinks.append(log_access)
    if settings.recent_requests:
        sinks.append(RequestRecorder(settings.recent_requests))
    if not sinks:
        return None

    def deliver(batch: Sequence[str]) -> None:
        for sink in sinks:
            sink(batch)

    return deliver


def build_worker(  # noqa: PLR0913 - the supervisor's descriptors and the metrics slot
    app: Hinoki,
    host: str,
    port: int,
    settings: ServerSettings,
    *,
    listener_fd: int | None = None,
    reuse_port: bool = False,
    parent: int | None = None,
    dev: bool = False,
    metrics_fd: int | None = None,
    metrics_dir: str | None = None,
    slot: int = 0,
    ready_fd: int | None = None,
) -> KisoWorker:
    """A worker for ``app`` with the limits of its ``server`` settings group
    (``None`` keeps the native default). ``metrics_fd``, ``metrics_dir`` and
    ``slot`` are what a supervisor gives a worker for the shared metrics
    listener; ``ready_fd`` is the pipe on which it reports that it serves."""
    limits: dict[str, Any] = {
        name: getattr(settings, name)
        for name in RUST_ONLY
        if getattr(settings, name) is not None
    }
    app_settings = getattr(app, "settings", None)
    return KisoWorker(
        app,
        host,
        port,
        backlog=settings.backlog,
        tls_cert_file=settings.tls_cert_file,
        tls_key_file=settings.tls_key_file,
        http2=settings.http2,
        listener_fd=listener_fd,
        reuse_port=reuse_port,
        parent=parent,
        ready_fd=ready_fd,
        access_log=access_style(settings, dev),
        access_log_skip=settings.access_log_skip,
        access_log_slow_ms=settings.access_log_slow_ms,
        access_log_sink=access_sink(settings, dev),
        server_timing=dev if settings.server_timing is None else settings.server_timing,
        compression=(
            app_settings.compression.native_options()
            if isinstance(app_settings, AppSettings)
            else None
        ),
        metrics=(
            app_settings.metrics.native_options(
                version=__version__, worker=slot, fd=metrics_fd, directory=metrics_dir
            )
            if isinstance(app_settings, AppSettings)
            else None
        ),
        **limits,
    )


def take_interrupts() -> None:
    """A supervised worker starts with SIGINT blocked (``Supervisor.spawn``).
    Unblock it before loading the target or starting a thread, since threads
    and the processes a handler starts inherit the mask. A no-op Python handler
    keeps a Ctrl-C during start-up from raising a traceback; unlike ``SIG_IGN``,
    exec resets it, so child processes still stop on Ctrl-C. ``run`` replaces
    it with the loop's handler."""
    signal.signal(signal.SIGINT, _ignore_interrupt)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})


def _ignore_interrupt(_number: int, _frame: object) -> None:
    return None


def run_wsgi_worker(args: argparse.Namespace, host: str, port: int) -> int:
    # Imported here: wsgiworker imports this module for the access log.
    from hinoki.runtime import wsgiworker  # noqa: PLC0415
    from hinoki.serve import load_wsgi_target  # noqa: PLC0415

    target = load_wsgi_target(args.target)
    settings = settings_of(target)
    return wsgiworker.run(
        target,
        host,
        port,
        settings,
        listener_fd=args.fd,
        reuse_port=args.reuse_port,
        parent=args.parent,
        dev=args.dev,
        metrics_fd=args.metrics_fd,
        metrics_dir=args.metrics_dir,
        slot=args.slot,
        ready_fd=args.ready_fd,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hinoki.runtime.kiso")
    parser.add_argument("target", help="module:application or module:factory")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--fd", type=int, help="an inherited listening socket (host and port ignored)"
    )
    parser.add_argument("--reuse-port", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--parent", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--dev", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--metrics-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--metrics-dir", help=argparse.SUPPRESS)
    parser.add_argument("--slot", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--ready-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument(
        "--interface",
        choices=("asgi", "wsgi"),
        default="asgi",
        help="how the target is called (default asgi)",
    )
    args = parser.parse_args(argv)
    if args.fd is not None and args.reuse_port:
        parser.error("--fd and --reuse-port cannot be used together")
    if args.parent is not None:
        take_interrupts()
        if os.getppid() != args.parent:
            return 1  # the supervisor died before this worker ran
    if args.fd is not None:
        os.set_inheritable(args.fd, False)  # nothing a handler starts keeps it
    if args.metrics_fd is not None:
        os.set_inheritable(args.metrics_fd, False)
    if args.ready_fd is not None:
        os.set_inheritable(args.ready_fd, False)
    host, port = (
        inherited_address(args.fd) if args.fd is not None else (args.host, args.port)
    )
    if args.interface == "wsgi":
        return run_wsgi_worker(args, host, port)
    found, is_factory = load_target(args.target)
    app = found() if is_factory else found
    settings = settings_of(app)
    core.configure_runtime(settings.runtime_threads)
    worker = build_worker(
        app,
        host,
        port,
        settings,
        listener_fd=args.fd,
        reuse_port=args.reuse_port,
        parent=args.parent,
        dev=args.dev,
        metrics_fd=args.metrics_fd,
        metrics_dir=args.metrics_dir,
        slot=args.slot,
        ready_fd=args.ready_fd,
    )
    return asyncio.run(worker.run())


if __name__ == "__main__":
    sys.exit(main())
