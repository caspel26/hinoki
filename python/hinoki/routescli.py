"""``hinoki routes``: every action of an application on one screen.

    hinoki routes blog.main:create_app
    hinoki routes blog.main:create_app --path /posts --method GET --format json

Lists method, path, handler and access in path order. Nothing is served and no
database is opened; the application is built the way ``hinoki check`` builds it.
"""

from __future__ import annotations

import argparse
import json
import sys

from hinoki.access import Access
from hinoki.checkcli import USAGE_ERRORS, build_app
from hinoki.routing import Route


def add_arguments(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    sub = commands.add_parser("routes", help="list the application's routes")
    sub.add_argument("target", help="module:app, or a factory returning the app")
    sub.add_argument("--path", default="", help="only paths starting with this")
    sub.add_argument("--method", default="", help="only this HTTP method")
    sub.add_argument("--format", choices=["text", "json"], default="text")


def access_of(route: Route) -> str:
    access: Access | None = route.access
    if access is None:
        return "public"
    permissions = ",".join(sorted(access.permissions))
    return (
        f"{type(access.auth).__name__}[{permissions}]"
        if permissions
        else (type(access.auth).__name__)
    )


def handler_of(route: Route) -> str:
    return f"{route.handler.__module__}.{route.name}"


def selected(routes: list[Route], path: str, method: str) -> list[Route]:
    chosen = [
        route
        for route in routes
        if route.path.startswith(path) and (not method or route.method == method)
    ]
    return sorted(chosen, key=lambda route: (route.path, route.method))


def run_routes(args: argparse.Namespace) -> int:
    try:
        app = build_app(args.target)
    except (*USAGE_ERRORS, SystemExit) as exc:
        print(f"error: cannot load {args.target}: {exc}", file=sys.stderr)
        return 2
    routes = selected(list(app.routes), args.path, args.method.upper())
    if args.format == "json":
        print(
            json.dumps(
                [
                    {
                        "method": route.method,
                        "path": route.path,
                        "handler": handler_of(route),
                        "access": access_of(route),
                        "status": route.status,
                    }
                    for route in routes
                ],
                indent=2,
            )
        )
        return 0
    if not routes:
        print("no routes match", file=sys.stderr)
        return 1
    width = max(len(route.path) for route in routes)
    for route in routes:
        print(
            f"{route.method:<7} {route.path:<{width}}  {access_of(route):<24} "
            f"{handler_of(route)}"
        )
    return 0
