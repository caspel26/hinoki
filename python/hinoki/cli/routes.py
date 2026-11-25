"""``hinoki routes``: every action of an application on one screen.

    hinoki routes                                      # the project's application
    hinoki routes notes.main:app --path /notes --method GET --format json

Lists method, path, handler and access in path order. Nothing is served and no
database is opened; the application is built the way ``hinoki check`` builds it.
``--lanes`` adds, for each route, whether the Rust server can run it natively
(generated CRUD) and, if not, the reason it uses the Python lane.
"""

from __future__ import annotations

import argparse
import sys

from hinoki import jsontext
from hinoki.access import Access
from hinoki.app import Hinoki
from hinoki.cli.check import USAGE_ERRORS, build_app
from hinoki.cli.reportui import lane_suffix, route_row
from hinoki.routing import Route
from hinoki.rules import qualified
from hinoki.runtime.crudplan import select_crud_plan
from hinoki.runtime.nativelane import lane_refusal
from hinoki.runtime.pipeline import Pipeline
from hinoki.terminal import Style


def add_arguments(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    sub = commands.add_parser("routes", help="list the application's routes")
    sub.add_argument(
        "target",
        nargs="?",
        help="module:app, or a factory returning the app (default: project app)",
    )
    sub.add_argument("--path", default="", help="only paths starting with this")
    sub.add_argument("--method", default="", help="only this HTTP method")
    sub.add_argument("--format", choices=["text", "json"], default="text")
    sub.add_argument(
        "--lanes",
        action="store_true",
        help="show the Rust server lane of each route (always present in JSON)",
    )


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
    """The action's rule subject, the name ``except_`` and waivers match."""
    return qualified(route)


def lanes_of(app: Hinoki) -> dict[int, tuple[str, str]]:
    """Lane and reason per route (by identity), as the Rust server would select
    them at worker startup (``native_dispatch`` off keeps ``async def`` handlers on
    Python); no database is opened."""
    pipeline = Pipeline(app)
    lanes: dict[int, tuple[str, str]] = {}
    for binding in pipeline.bindings:
        try:
            selection, reason = select_crud_plan(binding, pipeline)
        except Exception:
            selection, reason = None, "native_plan_build_failed"
        if selection is None and lane_refusal(binding, pipeline.events) is None:
            lanes[id(binding.route)] = ("native", "native_dispatch")
            continue
        lane = "native" if selection else "python"
        if binding.route.middleware:
            reason = "middleware"
        elif binding.route.deprecated:
            reason = "deprecated"
        lanes[id(binding.route)] = (lane, reason)
    return lanes


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
    lanes = lanes_of(app) if args.lanes or args.format == "json" else {}
    unknown = ("python", "not_generated_crud_action")
    if args.format == "json":
        print(
            jsontext.dumps(
                [
                    {
                        "method": route.method,
                        "path": route.path,
                        "handler": handler_of(route),
                        "access": access_of(route),
                        "status": route.status,
                        "lane": lanes.get(id(route), unknown)[0],
                        "lane_reason": lanes.get(id(route), unknown)[1],
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
    style = Style.detect(sys.stdout)
    width = max(len(route.path) for route in routes)
    access_width = max(24, *(len(access_of(route)) for route in routes))
    for route in routes:
        lane, reason = lanes.get(id(route), unknown)
        access, handler = access_of(route), handler_of(route)
        if style.color:
            suffix = lane_suffix(style, lane, reason) if args.lanes else ""
            row = route_row(
                style, route.method, route.path, width, access, handler, access_width
            )
            print(row + suffix)
            continue
        suffix = f"  [{lane}: {reason}]" if args.lanes else ""
        left = f"{route.method:<7} {route.path:<{width}}  {access:<{access_width}}"
        print(f"{left} {handler}{suffix}")
    return 0
