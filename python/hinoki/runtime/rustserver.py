"""One worker of the Rust HTTP/1.1 server (development entry point).

The listener, the HTTP parsing and the connection limits run on the native
tokio runtime (``core.RustServer``). Every request the native classifier does
not answer waits in a bounded queue; this module drains it on the asyncio loop,
runs ``Pipeline.handle`` for each one and hands the answer back to the
connection. The handlers and the pipeline run in Python, on the loop.

This slice serves requests without a body: a request that carries one is
refused with 501 by the native layer. ``server.trusted_proxies`` is refused at
startup, because the peer trust and the effective scheme are not ported yet.

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
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from hinoki import core
from hinoki.runtime.servercore import build_server_core
from hinoki.serve import load_target
from hinoki.settings import AppSettings

if TYPE_CHECKING:
    from hinoki.app import Hinoki

log = logging.getLogger("hinoki.runtime")

# Requests taken from the native queue per call; a short batch means it is empty.
DRAIN_BATCH = 256


class NoBody:
    """Body source of this slice: requests with a body never reach the pipeline."""

    async def read(self, limit: int) -> bytes:  # noqa: ARG002 - the protocol's signature
        return b""


def scope_of(request: core.PreparedRequest) -> dict[str, Any]:
    """The ASGI-shaped scope the pipeline reads, from a queued request."""
    return {
        "type": "http",
        "http_version": "1.1",
        "method": request.method,
        "scheme": "http",
        "path": unquote(request.path),
        # Non-ASCII bytes stay: the pipeline answers 400 bad_path_encoding for them.
        "raw_path": request.path.encode("utf-8"),
        "query_string": request.query,
        "headers": request.headers,
        "client": request.client,
    }


class RustWorker:
    """Runs one application on the Rust server inside the current event loop."""

    def __init__(
        self,
        app: Hinoki,
        host: str,
        port: int,
        *,
        max_connections: int = 10_000,
        header_read_timeout: float = 10.0,
    ) -> None:
        self.adapter = app.asgi()
        self.pipeline = self.adapter.pipeline
        self.host = host
        self.port = port
        self.max_connections = max_connections
        self.header_read_timeout = header_read_timeout
        # Starts not ready; the lifespan startup flips it.
        self.core = build_server_core(self.pipeline)
        self.server: core.RustServer | None = None
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
        if self.pipeline.trusted_proxies:
            raise RuntimeError(
                "the Rust server does not support server.trusted_proxies yet"
            )
        answer = await self._lifespan("startup")
        if answer["type"] != "lifespan.startup.complete":
            await self._finish_lifespan()
            raise RuntimeError(f"startup failed: {answer.get('message', '')}")
        runtime = self.pipeline.app.runtime
        try:
            server = core.RustServer.start(
                self.core,
                self.host,
                self.port,
                self.max_connections,
                self.header_read_timeout,
                runtime.shutdown_timeout,
                runtime.max_header_bytes,
            )
        except BaseException:
            await self._lifespan("shutdown")
            await self._finish_lifespan()
            raise
        self.server = server
        self.core.set_ready(True)
        asyncio.get_running_loop().add_reader(server.wake_fd, self._wake, server)
        return server.port

    def _wake(self, server: core.RustServer) -> None:
        """The loop's reader callback: clear the wake socket first, then drain
        the queue until it is empty (a short batch), as the queue requires. It
        keeps running while the server shuts down, so requests already queued
        still get their answer."""
        server.clear_wake()
        batch = self.core.drain(DRAIN_BATCH)
        while True:
            for request in batch:
                self._spawn(request)
            if len(batch) < DRAIN_BATCH:
                return
            batch = self.core.drain(DRAIN_BATCH)

    def _spawn(self, request: core.PreparedRequest) -> None:
        task = asyncio.create_task(self._serve(request))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _serve(self, request: core.PreparedRequest) -> None:
        """Answer one queued request. A request whose task is cancelled is
        dropped unanswered; the connection then gets a native 503."""
        try:
            response = await self.pipeline.handle(
                scope_of(request), NoBody(), request.request_id
            )
        except Exception:
            log.exception("request %s failed", request.request_id)
            request.respond(500, [(b"content-length", b"0")], b"")
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

    def _on_signal(self) -> None:
        if self.stopping.is_set():
            os._exit(1)  # a second signal does not wait
        self.stopping.set()

    async def run(self) -> int:
        """Serve until SIGINT or SIGTERM, then stop gracefully."""
        port = await self.start()
        loop = asyncio.get_running_loop()
        for number in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(number, self._on_signal)
        print(f"listening on http://{self.host}:{port}", flush=True)
        await self.stopping.wait()
        aborted = await self.stop()
        if aborted:
            log.warning("aborted %d connections at shutdown", aborted)
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hinoki.runtime.rustserver")
    parser.add_argument("target", help="module:application or module:factory")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    found, is_factory = load_target(args.target)
    app = found() if is_factory else found
    settings = getattr(app, "settings", None)
    if isinstance(settings, AppSettings):
        core.configure_runtime(settings.server.runtime_threads)
    return asyncio.run(RustWorker(app, args.host, args.port).run())


if __name__ == "__main__":
    sys.exit(main())
