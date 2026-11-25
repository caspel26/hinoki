"""``hinoki dev|serve [TARGET]``: run an application on Kiso, Hinoki's Rust server
(the default), or under Granian.

    hinoki dev                                         # the project's application
    hinoki dev   notes.main:app                        # reload on change
    hinoki serve notes.main:app                        # production-style, no reload
    hinoki serve notes.main:app --server granian       # Granian instead

``TARGET`` is ``module:name`` where ``name`` is an application (``Hinoki``) or
a function returning one; inside a project it defaults to ``project.application``
from ``hinoki.toml``. The host, port, worker count and server come from the
application's ``server`` settings group (``AppSettings.server``), and the
flags below override them. Kiso is part of the native extension;
Granian is an optional dependency (``pip install hinoki-api[server]``) and so is
``watchfiles``, which ``hinoki dev`` reloads with.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import inspect
import signal
import socket
import sys
from pathlib import Path
from typing import Any

from hinoki import __version__, core
from hinoki.app import Hinoki
from hinoki.routing import ROUTE_ATTR
from hinoki.runtime import banner
from hinoki.runtime.listener import (
    ListenError,
    check_port,
    open_listener,
    supports_reuse_port,
)
from hinoki.settings import MAX_WORKERS, AppSettings, ServerSettings, SettingsError
from hinoki.target import load_target as import_target
from hinoki.wsgi import WSGIAdapter


def add_arguments(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    for name, text in (
        ("dev", "run with reload on code changes"),
        ("serve", "run for production: no reload, workers from the settings"),
    ):
        sub = commands.add_parser(name, help=text)
        sub.add_argument(
            "target",
            nargs="?",
            help="module:application or module:factory (default: project app)",
        )
        sub.add_argument("--host", help="override server.host")
        sub.add_argument("--port", type=int, help="override server.port")
        sub.add_argument(
            "--server",
            choices=("granian", "kiso"),
            help="override server.server (default kiso)",
        )
        if name == "serve":
            sub.add_argument("--workers", type=int, help="override server.workers")
        sub.add_argument(
            "--interface",
            choices=("asgi", "wsgi"),
            help="how the server calls TARGET: a Hinoki application (asgi, the "
            "default) or a WSGI callable (wsgi); overrides server.interface",
        )


def load_wsgi_target(target: str) -> Any:
    """The WSGI callable named by ``target`` (``--interface wsgi``): a
    ``WSGIAdapter`` or any ``(environ, start_response)`` callable. Nothing
    builds it, so a factory is refused (exit status 2)."""
    try:
        found = import_target(target)
    except (ImportError, AttributeError, ValueError) as exc:
        raise SystemExit(f"error: cannot load {target}: {exc}") from None
    if isinstance(found, Hinoki):
        raise SystemExit(
            f"error: {target} is a Hinoki application (ASGI); for --interface wsgi "
            "name a WSGI callable, such as WSGIAdapter(app) from hinoki.wsgi"
        )
    if not callable(found):
        raise SystemExit(f"error: {target} is not a WSGI callable")
    if is_factory(found):
        print(
            f"error: {target} is a factory; for --interface wsgi name a WSGI "
            "callable, such as WSGIAdapter(app) from hinoki.wsgi",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return found


def is_factory(found: object) -> bool:
    """Whether ``found`` is called with no arguments to build an application
    (a declared action is not)."""
    if not callable(found) or hasattr(found, ROUTE_ATTR):
        return False
    try:
        parameters = inspect.signature(found).parameters.values()
    except (TypeError, ValueError):
        return False
    return all(
        p.default is not inspect.Parameter.empty
        or p.kind in {p.VAR_POSITIONAL, p.VAR_KEYWORD}
        for p in parameters
    )


def load_target(target: str) -> tuple[Any, bool]:
    """The named object and whether it is a factory (a callable, not an app)."""
    try:
        found = import_target(target)
    except (ImportError, AttributeError, ValueError) as exc:
        raise SystemExit(f"error: cannot load {target}: {exc}") from None
    if isinstance(found, Hinoki):
        return found, False
    if is_factory(found):
        return found, True
    raise SystemExit(
        f"error: {target} is neither an application nor a factory "
        "(for a WSGI callable, pass --interface wsgi)"
    )


def settings_of(app: object) -> ServerSettings:
    """The ``server`` group of an application, or of the application a
    ``WSGIAdapter`` wraps (defaults when there is none). A plain WSGI callable
    has no settings of its own, so they come from the environment."""
    if not isinstance(app, WSGIAdapter | Hinoki):
        return AppSettings.load().server
    if isinstance(app, WSGIAdapter):
        app = app.app
    settings = getattr(app, "settings", None)
    if isinstance(settings, AppSettings):
        return settings.server
    return ServerSettings()


def built_app(target: str) -> Hinoki:
    """The application of ``target``, built once (a factory is called)."""
    found, is_factory = load_target(target)
    try:
        return found() if is_factory else found  # type: ignore[no-any-return]
    except SettingsError as error:
        raise SystemExit(str(error)) from None


def server_settings(target: str) -> ServerSettings:
    """The ``server`` group of the application (building it once to read it)."""
    return settings_of(built_app(target))


def refuse(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def app_settings_of(app: object) -> AppSettings:
    """The settings of the application a runner hosts (the one a ``WSGIAdapter``
    wraps, for one), or the environment's for a plain WSGI callable, which has
    no settings of its own."""
    if not isinstance(app, WSGIAdapter | Hinoki):
        return AppSettings.load()
    if isinstance(app, WSGIAdapter):
        app = app.app
    settings = getattr(app, "settings", None)
    return settings if isinstance(settings, AppSettings) else AppSettings()


def compresses(app: object) -> bool:
    """Whether ``compression.enabled`` is on for ``app``."""
    return app_settings_of(app).compression.enabled


def measures(app: object) -> bool:
    """Whether ``metrics.listen`` is set for ``app``."""
    return app_settings_of(app).metrics.listen is not None


def kiso_only_refusal(chosen: str, compression: bool, metrics: bool) -> int | None:
    """The refusal for a Kiso-only setting that is on under another server."""
    if chosen == "kiso":
        return None
    for enabled, name, feature in (
        (compression, "compression.enabled", "compression"),
        (metrics, "metrics.listen", "metrics"),
    ):
        if enabled:
            return refuse(
                f"{name} is not supported by the {chosen} server "
                f"({feature} is Kiso only); unset it or choose --server kiso"
            )
    return None


def choose_server(
    args: argparse.Namespace,
    settings: ServerSettings,
    compression: bool = False,
    metrics: bool = False,
) -> str | int:
    """The server to run, or the exit status of a refusal: an unknown value, a
    setting the server cannot honour, or a Kiso that is not available.
    ``compression`` says whether ``compression.enabled`` is on and ``metrics``
    whether ``metrics.listen`` is set."""
    chosen = args.server or settings.server
    refusal = kiso_only_refusal(chosen, compression, metrics)
    if refusal is not None:
        return refusal
    conflicts = settings.conflicts(chosen)
    if conflicts:
        return refuse(
            f"server.{', server.'.join(conflicts)} "
            f"{'is' if len(conflicts) == 1 else 'are'} not supported by the "
            f"{chosen} server; unset it or choose "
            f"--server {'granian' if chosen == 'kiso' else 'kiso'}"
        )
    if chosen == "kiso" and not hasattr(core, "KisoServer"):
        return refuse(
            "Kiso is not available in this build of the native "
            "extension; use --server granian"
        )
    return chosen


def rust_refusal(args: argparse.Namespace, reload: bool) -> str | None:
    """Why the Rust server cannot run with these arguments, if it cannot."""
    asked = getattr(args, "workers", None)
    if asked is not None and not 0 <= asked <= MAX_WORKERS:
        return f"--workers must be 1 to {MAX_WORKERS} (0 uses server.workers)"
    if reload and importlib.util.find_spec("watchfiles") is None:
        return (
            "`hinoki dev` reloads with watchfiles, which is not installed; "
            "install the server extra: pip install 'hinoki-api[server]' "
            "(or run `hinoki serve`, which does not reload)"
        )
    return None


def run_inline(app: Hinoki, address: tuple[str, int], settings: ServerSettings) -> int:
    # Imported here: kiso imports this module for ``load_target``.
    from hinoki.runtime import kiso  # noqa: PLC0415

    core.configure_runtime(settings.runtime_threads)
    return asyncio.run(kiso.build_worker(app, *address, settings).run())


def stop_grace(app: Hinoki) -> float:
    """How long a worker may take to stop: the server drain, the background
    tasks, the executor, then each module, every step within ``shutdown_timeout``."""
    return (3 + len(app.modules)) * app.runtime.shutdown_timeout


def open_metrics(app: object, backlog: int) -> socket.socket | None:
    """The listener of ``metrics.listen``, opened by the supervisor once for
    every worker, or ``None`` while metrics are off."""
    metrics = app_settings_of(app).metrics
    address = metrics.address()
    if address is None:
        return None
    try:
        return open_listener(*address, backlog)
    except ListenError as error:
        raise ListenError(f"metrics.listen {metrics.listen}: {error}") from error


def rolls_on_hangup(settings: ServerSettings, reload: bool) -> bool:
    """Whether ``hinoki serve`` replaces its workers on SIGHUP: unless
    ``server.sighup`` is ``"stop"``, and where the system has the signal."""
    return not reload and settings.sighup != "stop" and hasattr(signal, "SIGHUP")


def use_reuse_port(
    settings: ServerSettings, workers: int, port: int, reload: bool
) -> bool:
    """Whether supervised workers each bind their own ``SO_REUSEPORT`` socket.

    Requires Linux, more than one worker, a non-zero port (port 0 falls back
    to a shared descriptor so every worker serves the same ephemeral port),
    and ``serve`` mode (``dev`` keeps one supervisor-owned socket across
    restarts). ``server.reuse_port = False`` forces the shared descriptor.
    """
    if settings.reuse_port is False:
        return False
    return not reload and workers > 1 and port != 0 and supports_reuse_port()


def run_supervised(
    args: argparse.Namespace,
    app: Hinoki,
    address: tuple[str, int],
    settings: ServerSettings,
    reload: bool,
) -> int:
    from hinoki.runtime.supervisor import Supervisor  # noqa: PLC0415

    workers = 1 if reload else (args.workers or settings.workers)
    reuse_port = use_reuse_port(settings, workers, address[1], reload)
    listener = open_listener(*address, settings.backlog, reuse_port=reuse_port)
    grace = stop_grace(app)
    scheme = "https" if settings.tls_cert_file else "http"
    supervisor = Supervisor(
        args.target,
        listener,
        workers,
        grace,
        reload=reload,
        scheme=scheme,
        metrics=open_metrics(app, settings.backlog),
        rolling=rolls_on_hangup(settings, reload),
        startup_timeout=app.runtime.startup_timeout,
        reuse_port=reuse_port,
    )
    supervisor.docs = app.docs_url
    return supervisor.run()


def run_rust(
    args: argparse.Namespace, reload: bool, app: Hinoki, settings: ServerSettings
) -> int:
    """Run on the Rust server: in this process for one worker, a supervisor
    with a process per worker otherwise (and always for ``dev``, which reloads
    by restarting the worker)."""
    reason = rust_refusal(args, reload)
    if reason is not None:
        return refuse(reason)
    address = (args.host or settings.host, args.port or settings.port)
    workers = 1 if reload else (getattr(args, "workers", None) or settings.workers)
    warn_key_access(settings)
    try:
        check_port(address[1])  # before any lifespan or worker starts
        banner.show(
            settings,
            version=__version__,
            dev=reload,
            workers=workers,
            scheme="https" if settings.tls_cert_file else "http",
        )
        if workers == 1 and not reload and not rolls_on_hangup(settings, reload):
            return run_inline(app, address, settings)
        return run_supervised(args, app, address, settings, reload)
    except ListenError as error:  # nothing was served
        return refuse(f"cannot listen on {address[0]}:{address[1]}: {error}")


def warn_key_access(settings: ServerSettings) -> None:
    """Say so when the TLS private key can be read by any local user. Not an
    error: a secret mounted by an orchestrator is often group-readable."""
    if settings.tls_key_file is None:
        return
    try:
        mode = Path(settings.tls_key_file).stat().st_mode
    except OSError:
        return
    if mode & 0o004:
        print(
            f"warning: {settings.tls_key_file} is readable by every local user; "
            "restrict it (chmod 600, or 640 for a service group)",
            file=sys.stderr,
        )


def granian_tls(settings: ServerSettings) -> dict[str, Any]:
    """Granian's TLS options, matching the Rust server: the same files, TLS 1.2
    and up (Granian's own default is 1.3) and HTTP/1.1 only, or HTTP/1.1 and
    HTTP/2 negotiated by ALPN when ``http2`` is on. Nothing changes for an
    application without TLS files."""
    if settings.tls_cert_file is None or settings.tls_key_file is None:
        return {}
    warn_key_access(settings)
    from granian.constants import HTTPModes, SSLProtocols  # noqa: PLC0415

    return {
        "ssl_cert": Path(settings.tls_cert_file),
        "ssl_key": Path(settings.tls_key_file),
        "ssl_protocol_min": SSLProtocols.tls12,
        "http": HTTPModes.auto if settings.http2 else HTTPModes.http1,
    }


def run_granian(
    args: argparse.Namespace,
    reload: bool,
    settings: ServerSettings,
    interface: str = "asgi",
) -> int:
    try:
        from granian import Granian  # noqa: PLC0415 - optional dependency
        from granian.constants import Interfaces  # noqa: PLC0415
    except ImportError:
        print(
            "error: Granian is not installed; install the server extra: "
            "pip install 'hinoki-api[server]'",
            file=sys.stderr,
        )
        return 2
    if reload and importlib.util.find_spec("watchfiles") is None:
        print(
            "error: `hinoki dev` reloads with watchfiles, which is not installed; "
            "install the server extra: pip install 'hinoki-api[server]' "
            "(or run `hinoki serve`, which does not reload)",
            file=sys.stderr,
        )
        return 2
    # A WSGI callable is never a factory: nothing builds it.
    is_factory = False if interface == "wsgi" else load_target(args.target)[1]
    server = Granian(
        args.target,
        address=args.host or settings.host,
        port=args.port or settings.port,
        interface=Interfaces.WSGI if interface == "wsgi" else Interfaces.ASGI,
        workers=1 if reload else (getattr(args, "workers", None) or settings.workers),
        runtime_threads=settings.runtime_threads,
        backlog=settings.backlog,
        log_access=settings.logs_access(reload),
        factory=is_factory,
        reload=reload,
        **granian_tls(settings),
    )
    server.serve()
    return 0


def wsgi_refusal(settings: ServerSettings) -> str | None:
    """Why Kiso cannot host a WSGI callable with these settings, if it cannot."""
    if settings.native_dispatch is not None:
        return (
            "server.native_dispatch does not apply to --interface wsgi (the native "
            "lane serves Hinoki routes, which a WSGI callable does not have); unset it"
        )
    if settings.trusted_proxies:
        return (
            "server.trusted_proxies is not supported with --interface wsgi on Kiso: "
            "the client address is the TCP peer; unset it, or use --server granian"
        )
    return None


def run_wsgi_kiso(
    args: argparse.Namespace, reload: bool, target: Any, settings: ServerSettings
) -> int:
    """Host a WSGI callable on Kiso: in this process for one worker, a supervisor
    with a process per worker otherwise (and always for ``dev``)."""
    reason = wsgi_refusal(settings) or rust_refusal(args, reload)
    if reason is not None:
        return refuse(reason)
    address = (args.host or settings.host, args.port or settings.port)
    workers = 1 if reload else (getattr(args, "workers", None) or settings.workers)
    scheme = "https" if settings.tls_cert_file else "http"
    try:
        check_port(address[1])
        banner.show(
            settings,
            version=__version__,
            dev=reload,
            workers=workers,
            scheme=scheme,
            interface="wsgi",
        )
        if workers == 1 and not reload and not rolls_on_hangup(settings, reload):
            # Imported here: wsgiworker imports the Rust worker module.
            from hinoki.runtime import wsgiworker  # noqa: PLC0415

            return wsgiworker.run(target, *address, settings, dev=reload)
        from hinoki.runtime.supervisor import Supervisor  # noqa: PLC0415

        reuse_port = use_reuse_port(settings, workers, address[1], reload)
        listener = open_listener(*address, settings.backlog, reuse_port=reuse_port)
        grace = stop_grace_of(target)
        return Supervisor(
            args.target,
            listener,
            workers,
            grace,
            reload=reload,
            scheme=scheme,
            interface="wsgi",
            metrics=open_metrics(target, settings.backlog),
            rolling=rolls_on_hangup(settings, reload),
            startup_timeout=app_settings_of(target).lifecycle.startup_timeout,
            reuse_port=reuse_port,
        ).run()
    except ListenError as error:
        return refuse(f"cannot listen on {address[0]}:{address[1]}: {error}")


def stop_grace_of(target: Any) -> float:
    """How long a WSGI worker may take to stop: the drain, then the adapter's
    own shutdown (its application's modules, when it wraps one)."""
    wrapped = target.app if isinstance(target, WSGIAdapter) else None
    if isinstance(wrapped, Hinoki):
        return stop_grace(wrapped)
    settings = AppSettings() if isinstance(target, WSGIAdapter) else AppSettings.load()
    return 3 * settings.lifecycle.shutdown_timeout


def run_wsgi(args: argparse.Namespace, reload: bool) -> int:
    """``--interface wsgi``: a WSGI callable on Kiso or on Granian."""
    target = load_wsgi_target(args.target)
    try:
        settings = settings_of(target)
    except SettingsError as error:
        return refuse(str(error))
    chosen = choose_server(args, settings, compresses(target), measures(target))
    if isinstance(chosen, int):
        return chosen
    if chosen == "kiso":
        return run_wsgi_kiso(args, reload, target, settings)
    del target
    return run_granian(args, reload, settings, interface="wsgi")


def wanted_interface(args: argparse.Namespace) -> str:
    """The interface to serve: ``--interface``, else ``server.interface`` of a
    ``WSGIAdapter`` target, else ASGI."""
    asked = getattr(args, "interface", None)
    if asked:
        return str(asked)
    try:
        found = import_target(args.target)
    except (ImportError, AttributeError, ValueError):
        return "asgi"  # load_target reports it
    if isinstance(found, WSGIAdapter) and settings_of(found).interface == "wsgi":
        return "wsgi"
    return "asgi"


def run(args: argparse.Namespace, reload: bool) -> int:
    if wanted_interface(args) == "wsgi":
        return run_wsgi(args, reload)
    return run_asgi(args, reload)


def run_asgi(args: argparse.Namespace, reload: bool) -> int:
    app = built_app(args.target)
    settings = settings_of(app)
    if settings.interface == "wsgi" and getattr(args, "interface", None) is None:
        return refuse(
            "server.interface is wsgi, but TARGET is a Hinoki application (ASGI); "
            "name a WSGI callable such as WSGIAdapter(app), or pass --interface asgi"
        )
    chosen = choose_server(args, settings, compresses(app), measures(app))
    if isinstance(chosen, int):
        return chosen
    if chosen == "kiso":
        return run_rust(args, reload, app, settings)
    del app  # Granian's workers build their own; do not keep this one alive
    return run_granian(args, reload, settings)


def run_dev(args: argparse.Namespace) -> int:
    return run(args, reload=True)


def run_serve(args: argparse.Namespace) -> int:
    return run(args, reload=False)
