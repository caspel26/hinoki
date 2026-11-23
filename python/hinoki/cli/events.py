"""``hinoki events``: the event handlers an application registered.

    hinoki events                                   # the project's application
    hinoki events notes.main:app --format json

Lists every handler in execution order with its event, phase, collection and
whether it is ``async``, then the ``events`` limits and, inside a project, the
``events`` target each app declares in ``hinoki.toml``. Nothing is served, no
database is opened and nothing is written; the application is built the way
``hinoki check`` builds it.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from hinoki import jsontext
from hinoki.cli.check import USAGE_ERRORS, build_app
from hinoki.events import EventBus, Registration
from hinoki.project import ProjectError, find_manifest_or_none, load_manifest


def add_arguments(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    sub = commands.add_parser(
        "events", help="list the application's event handlers and limits"
    )
    sub.add_argument(
        "target",
        nargs="?",
        help="module:app, or a factory returning the app (default: project app)",
    )
    sub.add_argument("--format", choices=["text", "json"], default="text")


def registration_row(item: Registration) -> dict[str, Any]:
    return {
        "event": item.event_name,
        "phase": item.phase.value,
        "handler": item.name,
        "collection": item.collection,
        "async": item.is_async,
    }


def limits_of(bus: EventBus) -> dict[str, Any]:
    return {
        "handler_timeout": bus.handler_timeout,
        "max_handlers": bus.max_handlers,
        "max_depth": bus.max_depth,
    }


def declared_apps() -> list[dict[str, str]]:
    """The ``events`` target of each app of the project's ``hinoki.toml``, in
    dependency order; empty outside a project. The manifest is only read."""
    path = find_manifest_or_none()
    if path is None:
        return []
    try:
        manifest = load_manifest(path)
    except ProjectError:
        return []
    return [
        {"app": app.name, "events": app.events}
        for app in manifest.ordered_apps()
        if app.events
    ]


def text_report(
    rows: list[dict[str, Any]], limits: dict[str, Any], apps: list[dict[str, str]]
) -> list[str]:
    lines: list[str] = []
    if rows:
        width = max(len(row["event"]) for row in rows)
        phase = max(len(row["phase"]) for row in rows)
        for row in rows:
            kind = "async" if row["async"] else "sync"
            lines.append(
                f"{row['event']:<{width}}  {row['phase']:<{phase}}  "
                f"{row['collection'] or '-':<12} {kind:<5}  {row['handler']}"
            )
    else:
        lines.append("no event handlers registered")
    lines.append("")
    lines.append(
        "limits: "
        + ", ".join(f"events.{name}={value}" for name, value in limits.items())
    )
    lines.extend(
        f"declared in hinoki.toml: {app['app']} -> {app['events']}" for app in apps
    )
    return lines


def run_events(args: argparse.Namespace) -> int:
    try:
        app = build_app(args.target)
    except (*USAGE_ERRORS, SystemExit) as exc:
        print(f"error: cannot load {args.target}: {exc}", file=sys.stderr)
        return 2
    rows = [registration_row(item) for item in app.events.registrations]
    limits = limits_of(app.events)
    apps = declared_apps()
    if args.format == "json":
        print(
            jsontext.dumps({"handlers": rows, "limits": limits, "apps": apps}, indent=2)
        )
        return 0
    print("\n".join(text_report(rows, limits, apps)))
    return 0
