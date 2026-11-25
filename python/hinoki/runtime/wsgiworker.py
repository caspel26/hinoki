"""One worker of Kiso hosting a WSGI callable (``--interface wsgi``).

The listener, TLS, HTTP parsing, the request body limit, the access log and the
admission bound run in Rust (``core.WsgiServer``); the application and the
response pieces are Python objects on a bounded pool of threads, so this path is
not native. See ``hinoki.runtime.wsgibridge`` for the Python side.

``hinoki serve --interface wsgi`` runs one worker in the CLI process, or starts
this worker as a process per worker from ``hinoki.runtime.supervisor`` (always
under ``hinoki dev``). Without an inherited socket it binds itself.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from typing import Any

from hinoki import __version__, core
from hinoki.runtime import wsgibridge
from hinoki.runtime.kiso import access_sink, access_style
from hinoki.runtime.listener import open_listener, signal_ready
from hinoki.settings import (
    AppSettings,
    ExecutionSettings,
    LifecycleSettings,
    ServerSettings,
)
from hinoki.wsgi import WSGIAdapter

log = logging.getLogger("hinoki.runtime")
# The Rust server limits that apply to a hosted WSGI callable.
LIMITS = (
    "max_connections",
    "header_read_timeout",
    "body_read_timeout",
    "write_timeout",
)


def app_settings(target: object) -> AppSettings:
    """The settings a ``WSGIAdapter`` brings from its application; a plain
    callable has none of its own, so they come from the environment."""
    if not isinstance(target, WSGIAdapter):
        return AppSettings.load()
    settings = getattr(target.app, "settings", None)
    return settings if isinstance(settings, AppSettings) else AppSettings()


def start_server(  # noqa: PLR0913 - the metrics listener and slot ride along
    target: Any,
    host: str,
    port: int,
    settings: ServerSettings,
    *,
    listener_fd: int | None,
    reuse_port: bool = False,
    dev: bool,
    metrics_fd: int | None = None,
    metrics_dir: str | None = None,
    slot: int = 0,
) -> core.WsgiServer:
    """Start the server for ``target``; errors surface here, before any request."""
    app = app_settings(target)
    execution: ExecutionSettings = app.execution
    lifecycle: LifecycleSettings = app.lifecycle
    if listener_fd is None:
        listener_fd = open_listener(
            host, port, settings.backlog, reuse_port=reuse_port
        ).detach()
    limits = {
        name: getattr(settings, name)
        for name in LIMITS
        if getattr(settings, name) is not None
    }
    return core.WsgiServer.start(
        wsgibridge.Bridge(target, host, port),
        host,
        port,
        threads=execution.sync_workers,
        queue=execution.sync_queue,
        max_body_bytes=execution.max_body_bytes,
        request_timeout=execution.request_timeout,
        shutdown_timeout=lifecycle.shutdown_timeout,
        max_header_bytes=execution.max_header_bytes,
        fd=listener_fd,
        tls_cert=settings.tls_cert_file,
        tls_key=settings.tls_key_file,
        http2=settings.http2,
        access_log=access_style(settings, dev),
        access_log_skip=list(settings.access_log_skip or ()) or None,
        access_log_slow_ms=settings.access_log_slow_ms,
        access_log_sink=access_sink(settings, dev),
        server_timing=dev if settings.server_timing is None else settings.server_timing,
        compression=app.compression.native_options(),
        metrics=app.metrics.native_options(
            version=__version__, worker=slot, fd=metrics_fd, directory=metrics_dir
        ),
        **limits,
    )


def wait_for_stop(parent: int | None, ready_fd: int | None = None) -> None:
    """Block until SIGTERM (or SIGINT outside a supervisor). A second signal
    does not wait; a supervised worker leaves SIGINT and SIGHUP to its
    supervisor. ``ready_fd`` is told once the handlers are in place."""
    stopping = threading.Event()

    def on_signal(_number: int, _frame: object) -> None:
        if stopping.is_set():
            if parent is None:
                os._exit(1)
            return
        stopping.set()

    def ignore(_number: int, _frame: object) -> None:
        return None

    interrupt = on_signal if parent is None else ignore
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGHUP, interrupt)
    if ready_fd is not None:
        signal_ready(ready_fd)
    while not stopping.wait(0.5):
        if parent is not None and os.getppid() != parent:
            return  # the supervisor is gone


def run(  # noqa: PLR0913 - the metrics listener and slot ride along
    target: Any,
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
) -> int:
    """Serve ``target`` until a stop signal, then drain and close it."""
    core.configure_runtime(settings.runtime_threads)
    if isinstance(target, WSGIAdapter):
        target.start()  # startup errors stop the worker before it listens
    try:
        server = start_server(
            target,
            host,
            port,
            settings,
            listener_fd=listener_fd,
            reuse_port=reuse_port,
            dev=dev,
            metrics_fd=metrics_fd,
            metrics_dir=metrics_dir,
            slot=slot,
        )
        try:
            if parent is None:
                shown = f"[{host}]" if ":" in host else host
                scheme = "https" if settings.tls_cert_file else "http"
                print(f"listening on {scheme}://{shown}:{server.port}", flush=True)
            wait_for_stop(parent, ready_fd)
        finally:
            aborted = server.shutdown()
        if aborted:
            log.warning("aborted %d connections at shutdown", aborted)
    finally:
        if isinstance(target, WSGIAdapter):
            target.close()
    return 0
