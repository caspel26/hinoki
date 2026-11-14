"""``hinoki settings``: show where every setting comes from.

    hinoki settings blog.settings:BlogSettings --prefix BLOG_ --dotenv .env
    hinoki settings blog.settings:BlogSettings --prefix BLOG_ --example

Prints each variable with its effective value (secrets as ``***``) and its
origin: ``override``, ``environment``, ``.env``, ``toml``, ``file`` (a ``*_FILE``
variable) or ``default``. An invalid configuration prints every problem, with
variable names and reasons but never values, and exits 1.
"""

from __future__ import annotations

import argparse
import json
import sys

from hinoki.checkcli import USAGE_ERRORS, build_app
from hinoki.settings import Settings, SettingsError
from hinoki.target import load_target


def add_arguments(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    sub = commands.add_parser(
        "settings", help="show the effective settings and where each value comes from"
    )
    sub.add_argument("target", help="module:SettingsClass, or the app")
    sub.add_argument(
        "--prefix", default=None, help="variable prefix (default: the class's own)"
    )
    sub.add_argument("--dotenv", help="a .env file (default: the class's own)")
    sub.add_argument("--toml", help="a TOML file (default: the class's own)")
    sub.add_argument("--format", choices=["text", "json"], default="text")
    sub.add_argument(
        "--example", action="store_true", help="print a .env.example instead"
    )


def load_class(target: str) -> type[Settings]:
    try:
        found = load_target(target)
    except (ImportError, AttributeError, ValueError) as exc:
        raise SystemExit(f"error: cannot load {target}: {exc}") from None
    if isinstance(found, type) and issubclass(found, Settings):
        return found
    try:  # an application (or a factory): the class of its settings
        app = build_app(target)
    except (*USAGE_ERRORS, SystemExit):
        raise SystemExit(f"error: {target} is not a Settings class or an app") from None
    if isinstance(app.settings, Settings):
        return type(app.settings)
    raise SystemExit(f"error: the app {target} has no typed settings")


def run_settings(args: argparse.Namespace) -> int:
    cls = load_class(args.target)
    if args.example:
        print(cls.example(args.prefix))
        return 0
    try:
        rows = cls.explain(prefix=args.prefix, dotenv=args.dotenv, toml=args.toml)
    except SettingsError as error:
        print(f"error: {error}", file=sys.stderr)
        print(
            "  fix: set the named variables or correct the selected settings "
            "source, then rerun "
            "`hinoki settings`",
            file=sys.stderr,
        )
        return 1
    if args.format == "json":
        print(
            json.dumps(
                [{"key": r.key, "value": r.value, "origin": r.origin} for r in rows]
            )
        )
        return 0
    width = max((len(r.key) for r in rows), default=0)
    for row in rows:
        print(f"{row.key:<{width}}  {row.origin:<11}  {row.value}")
    return 0
