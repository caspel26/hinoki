"""``hinoki dev|serve TARGET``: run an application under Granian (the default)
or the Rust server.

    hinoki dev   blog.main:create_app                  # reload on change
    hinoki serve blog.main:create_app                  # production-style, no reload
    hinoki serve blog.main:create_app --server rust    # the Rust HTTP/1.1 server

``TARGET`` is ``module:name`` where ``name`` is an application (``Hinoki``) or
a function returning one. The host, port, worker count and server come from the
application's ``server`` settings group (``AppSettings.server``), and the
flags below override them. Granian is an optional dependency
(``pip install hinoki[server]``); nothing else in the framework needs it. The
Rust server is part of the native extension, but ``hinoki dev`` reloads with
``watchfiles``, which comes with the same extra.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import inspect
import sys
from typing import Any

from hinoki import core
from hinoki.app import Hinoki
from hinoki.routing import ROUTE_ATTR
from hinoki.settings import AppSettings, ServerSettings, SettingsError
from hinoki.target import load_target as import_target


def add_arguments(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    for name, text in (
        ("dev", "run with reload on code changes"),
        ("serve", "run for production: no reload, workers from the settings"),
    ):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("target", help="module:application or module:factory")
        sub.add_argument("--host", help="override server.host")
        sub.add_argument("--port", type=int, help="override server.port")
        sub.add_argument(
            "--server",
            choices=("granian", "rust"),
            help="override server.server (default granian)",
        )
        if name == "serve":
            sub.add_argument("--workers", type=int, help="override server.workers")


def load_target(target: str) -> tuple[Any, bool]:
    """The named object and whether it is a factory (a callable, not an app)."""
    try:
        found = import_target(target)
    except (ImportError, AttributeError, ValueError) as exc:
        raise SystemExit(f"error: cannot load {target}: {exc}") from None
    if isinstance(found, Hinoki):
        return found, False
    if (
        callable(found)
        and not hasattr(found, ROUTE_ATTR)  # a declared action is not a factory
        and all(
            p.default is not inspect.Parameter.empty
            or p.kind in {p.VAR_POSITIONAL, p.VAR_KEYWORD}
            for p in inspect.signature(found).parameters.values()
        )  # callable with no arguments
    ):
        return found, True
    raise SystemExit(f"error: {target} is neither an application nor a factory")


def settings_of(app: Hinoki) -> ServerSettings:
    """The ``server`` group of an application (defaults when it has none)."""
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


def choose_server(args: argparse.Namespace, settings: ServerSettings) -> str | int:
    """The server to run, or the exit status of a refusal: an unknown value, a
    setting the server cannot honour, or a Rust server that is not available."""
    chosen = args.server or settings.server
    conflicts = settings.conflicts(chosen)
    if conflicts:
        return refuse(
            f"server.{', server.'.join(conflicts)} "
            f"{'is' if len(conflicts) == 1 else 'are'} not supported by the "
            f"{chosen} server; unset it or choose "
            f"--server {'granian' if chosen == 'rust' else 'rust'}"
        )
    if chosen == "rust" and not hasattr(core, "RustServer"):
        return refuse(
            "the Rust server is not available in this build of the native "
            "extension; use --server granian"
        )
    return chosen


def run_rust(
    args: argparse.Namespace, reload: bool, app: Hinoki, settings: ServerSettings
) -> int:
    """Run on the Rust server: in this process for one worker, a supervisor
    with a process per worker otherwise (and always for ``dev``, which reloads
    by restarting the worker)."""
    if reload and importlib.util.find_spec("watchfiles") is None:
        return refuse(
            "`hinoki dev` reloads with watchfiles, which is not installed; "
            "install the server extra: pip install 'hinoki[server]' "
            "(or run `hinoki serve`, which does not reload)"
        )
    # Imported here: rustserver imports this module for ``load_target``.
    from hinoki.runtime import rustserver  # noqa: PLC0415
    from hinoki.runtime.listener import open_listener  # noqa: PLC0415
    from hinoki.runtime.supervisor import Supervisor  # noqa: PLC0415

    host = args.host or settings.host
    port = args.port or settings.port
    workers = 1 if reload else (getattr(args, "workers", None) or settings.workers)
    if workers == 1 and not reload:
        core.configure_runtime(settings.runtime_threads)
        return asyncio.run(rustserver.build_worker(app, host, port, settings).run())
    try:
        listener = open_listener(host, port, settings.backlog)
    except OSError as error:
        return refuse(f"cannot listen on {host}:{port}: {error}")
    grace = app.runtime.shutdown_timeout
    return Supervisor(args.target, listener, workers, grace, reload=reload).run()


def run_granian(
    args: argparse.Namespace, reload: bool, settings: ServerSettings
) -> int:
    try:
        from granian import Granian  # noqa: PLC0415 - optional dependency
        from granian.constants import Interfaces  # noqa: PLC0415
    except ImportError:
        print(
            "error: Granian is not installed; install the server extra: "
            "pip install 'hinoki[server]'",
            file=sys.stderr,
        )
        return 2
    if reload and importlib.util.find_spec("watchfiles") is None:
        print(
            "error: `hinoki dev` reloads with watchfiles, which is not installed; "
            "install the server extra: pip install 'hinoki[server]' "
            "(or run `hinoki serve`, which does not reload)",
            file=sys.stderr,
        )
        return 2
    _, is_factory = load_target(args.target)
    server = Granian(
        args.target,
        address=args.host or settings.host,
        port=args.port or settings.port,
        interface=Interfaces.ASGI,
        workers=1 if reload else (getattr(args, "workers", None) or settings.workers),
        runtime_threads=settings.runtime_threads,
        backlog=settings.backlog,
        log_access=settings.access_log,
        factory=is_factory,
        reload=reload,
    )
    server.serve()
    return 0


def run(args: argparse.Namespace, reload: bool) -> int:
    app = built_app(args.target)
    settings = settings_of(app)
    chosen = choose_server(args, settings)
    if isinstance(chosen, int):
        return chosen
    if chosen == "rust":
        return run_rust(args, reload, app, settings)
    return run_granian(args, reload, settings)


def run_dev(args: argparse.Namespace) -> int:
    return run(args, reload=True)


def run_serve(args: argparse.Namespace) -> int:
    return run(args, reload=False)
