"""One worker of the Rust HTTP/1.1 server.

The listener, the HTTP parsing and the connection limits run on the native
tokio runtime (``core.RustServer``). Every request the native classifier does
not answer waits in a bounded queue; this module drains it on the asyncio loop,
runs ``Pipeline.handle`` for each one and hands the answer back to the
connection. The handlers and the pipeline run in Python, on the loop.

A request body stays in the native connection until the pipeline asks for it
(after authentication and negotiation, as on the ASGI path); it is read by the
native layer with the same limits and error codes. ``server.trusted_proxies``
applies as on the ASGI path: the native layer matches the TCP peer against it
(for the request id and the policy headers of its own answers) and the pipeline
resolves the forwarded scheme and client of queued requests from the same peer.

``hinoki serve --server rust`` runs one worker in the CLI process, or, for
several workers (and always under ``hinoki dev``), starts this module as a
process per worker from ``hinoki.runtime.supervisor``, which owns the listening
socket and passes it down. Run by hand it binds itself:

    python -m hinoki.runtime.rustserver module:app --port 8000
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from collections.abc import Sequence
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from hinoki import core
from hinoki.errors import InvalidBody
from hinoki.runtime.listener import inherited_address, open_listener
from hinoki.runtime.responses import HTTPError
from hinoki.runtime.servercore import build_server_core
from hinoki.serve import load_target, settings_of
from hinoki.settings import RUST_ONLY, ServerSettings

if TYPE_CHECKING:
    from hinoki.app import Hinoki

log = logging.getLogger("hinoki.runtime")

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


def scope_of(
    request: core.PreparedRequest, server: tuple[str, int], scheme: str = "http"
) -> dict[str, Any]:
    """The ASGI-shaped scope the pipeline reads, from a queued request.
    ``server`` is where the worker listens; ``Request.host`` falls back to it
    for an HTTP/1.0 request without a ``Host`` header. ``scheme`` is ``https``
    when the worker terminates TLS itself."""
    return {
        "type": "http",
        "http_version": "1.1",
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


class RustWorker:
    """Runs one application on the Rust server inside the current event loop."""

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
        backlog: int = 1024,
        listener_fd: int | None = None,
        parent: int | None = None,
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
        self.backlog = backlog
        # An inherited listening descriptor (a supervisor owns the socket); the
        # native server takes it over. Without one, the worker binds itself.
        self.listener_fd = listener_fd
        # The pid of the supervisor that started this worker, if any: the worker
        # stops when its parent is no longer that process.
        self.parent = parent
        self.timer: asyncio.TimerHandle | None = None
        # Starts not ready; the lifespan startup flips it.
        self.core = build_server_core(self.pipeline)
        self.server: core.RustServer | None = None
        self.address = (host, port)
        self.scheme = "https" if tls_cert_file else "http"
        self.tasks: set[asyncio.Task[None]] = set()
        self.stopping = asyncio.Event()
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._lifespan_task: asyncio.Task[None] | None = None

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
            if self.listener_fd is None:
                # Bound only now, after the startup succeeded: a failed startup
                # leaves nothing listening.
                listener = open_listener(self.host, self.port, self.backlog)
                self.listener_fd = listener.detach()
            fd, self.listener_fd = self.listener_fd, None  # taken, even on failure
            server = core.RustServer.start(
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
            )
        except BaseException:
            await self._lifespan("shutdown")
            await self._finish_lifespan()
            raise
        self.server = server
        self.address = (self.host, server.port)
        self.core.set_ready(True)
        loop = asyncio.get_running_loop()
        loop.add_reader(server.wake_fd, self._wake, server)
        self.timer = loop.call_later(HOUSEKEEPING, self._tick)
        return server.port

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
        if self.parent is not None and os.getppid() != self.parent:
            log.error("the supervisor is gone; stopping")
            self.stopping.set()

    def _wake(self, server: core.RustServer) -> None:
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
        task = asyncio.create_task(self._serve(request))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

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

    async def _serve(self, request: core.PreparedRequest) -> None:
        """Answer one queued request. A request whose task is cancelled (at
        shutdown, after ``shutdown_timeout``) is dropped unanswered: its
        connection is closed by the native layer without a response, so the
        client sees EOF, not a 503."""
        if request.id_ignored:
            log.warning("ignored invalid request ID from trusted proxy")
        route = None if request.action is None else (request.action, request.params)
        try:
            response = await self.pipeline.handle(
                scope_of(request, self.address, self.scheme),
                RustBody(request),
                request.request_id,
                route,
            )
        except Exception:
            log.exception("request %s failed", request.request_id)
            self._fail(request)
            return
        body = b"" if request.method == "HEAD" else response.body
        request.respond(response.status, response.headers, body)

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
        try:
            aborted = await server.shutdown()
        finally:
            asyncio.get_running_loop().remove_reader(server.wake_fd)
            await self._cancel_requests()
            self.core.set_ready(False)
            await self._lifespan("shutdown")
            await self._finish_lifespan()
        return aborted

    async def _cancel_requests(self) -> None:
        for task in list(self.tasks):
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
        """Serve until SIGINT or SIGTERM, then stop gracefully."""
        port = await self.start()
        try:
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGTERM, self._on_signal)
            interrupt = self._on_signal if self.parent is None else self._ignore_signal
            loop.add_signal_handler(signal.SIGINT, interrupt)
            loop.add_signal_handler(signal.SIGHUP, interrupt)
            if self.parent is None:
                shown = f"[{self.host}]" if ":" in self.host else self.host
                print(f"listening on {self.scheme}://{shown}:{port}", flush=True)
            await self.stopping.wait()
        finally:
            aborted = await self.stop()
        if aborted:
            log.warning("aborted %d connections at shutdown", aborted)
        return 0


def build_worker(
    app: Hinoki,
    host: str,
    port: int,
    settings: ServerSettings,
    *,
    listener_fd: int | None = None,
    parent: int | None = None,
) -> RustWorker:
    """A worker for ``app`` with the limits of its ``server`` settings group
    (``None`` keeps the native default)."""
    limits: dict[str, Any] = {
        name: getattr(settings, name)
        for name in RUST_ONLY
        if getattr(settings, name) is not None
    }
    return RustWorker(
        app,
        host,
        port,
        backlog=settings.backlog,
        tls_cert_file=settings.tls_cert_file,
        tls_key_file=settings.tls_key_file,
        listener_fd=listener_fd,
        parent=parent,
        **limits,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hinoki.runtime.rustserver")
    parser.add_argument("target", help="module:application or module:factory")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--fd", type=int, help="an inherited listening socket (host and port ignored)"
    )
    parser.add_argument("--parent", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.parent is not None and os.getppid() != args.parent:
        return 1  # the supervisor died before this worker ran
    if args.fd is not None:
        os.set_inheritable(args.fd, False)  # nothing a handler starts keeps it
    host, port = (
        inherited_address(args.fd) if args.fd is not None else (args.host, args.port)
    )
    found, is_factory = load_target(args.target)
    app = found() if is_factory else found
    settings = settings_of(app)
    core.configure_runtime(settings.runtime_threads)
    worker = build_worker(
        app, host, port, settings, listener_fd=args.fd, parent=args.parent
    )
    return asyncio.run(worker.run())


if __name__ == "__main__":
    sys.exit(main())
