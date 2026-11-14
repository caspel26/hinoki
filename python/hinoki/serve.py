"""``hinoki dev|serve TARGET``: run an application under Granian.

    hinoki dev   blog.main:create_app     # reload on change
    hinoki serve blog.main:create_app     # production-style, no reload

``TARGET`` is ``module:name`` where ``name`` is an application (``Hinoki``) or
a function returning one. The host, port and worker count come from the
application's ``server`` settings group (``AppSettings.server``), and the
flags below override them. Granian is an optional dependency
(``pip install hinoki[server]``); nothing else in the framework needs it.
"""

from __future__ import annotations

import argparse
import inspect
import sys
from typing import Any

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


def server_settings(target: str) -> ServerSettings:
    """The ``server`` group of the application (building it once to read it)."""
    found, is_factory = load_target(target)
    try:
        app = found() if is_factory else found
    except SettingsError as error:
        raise SystemExit(str(error)) from None
    settings = getattr(app, "settings", None)
    if isinstance(settings, AppSettings):
        return settings.server
    return ServerSettings()


def run(args: argparse.Namespace, reload: bool) -> int:
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
    _, is_factory = load_target(args.target)
    settings = server_settings(args.target)
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


def run_dev(args: argparse.Namespace) -> int:
    return run(args, reload=True)


def run_serve(args: argparse.Namespace) -> int:
    return run(args, reload=False)
