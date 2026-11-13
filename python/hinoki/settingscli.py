"""``python -m hinoki settings``: show where every setting comes from.

    python -m hinoki settings blog.settings:BlogSettings --prefix BLOG_ --dotenv .env
    python -m hinoki settings blog.settings:BlogSettings --prefix BLOG_ --example

Prints each variable with its effective value (secrets as ``***``) and its
origin: ``override``, ``environment``, ``.env``, ``file`` (a ``*_FILE``
variable) or ``default``. An invalid configuration prints every problem, with
variable names and reasons but never values, and exits 1.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys

from hinoki.settings import Settings, SettingsError


def add_arguments(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    sub = commands.add_parser(
        "settings", help="show the effective settings and where each value comes from"
    )
    sub.add_argument("target", help="module:SettingsClass")
    sub.add_argument("--prefix", default="", help="environment variable prefix")
    sub.add_argument("--dotenv", help="a .env file to read")
    sub.add_argument("--format", choices=["text", "json"], default="text")
    sub.add_argument(
        "--example", action="store_true", help="print a .env.example instead"
    )


def load_class(target: str) -> type[Settings]:
    module_name, _, name = target.partition(":")
    if not module_name or not name:
        raise SystemExit("error: target is module:SettingsClass")
    try:
        found = getattr(importlib.import_module(module_name), name)
    except (ImportError, AttributeError) as exc:
        raise SystemExit(f"error: cannot load {target}: {exc}") from None
    if not (isinstance(found, type) and issubclass(found, Settings)):
        raise SystemExit(f"error: {target} is not a Settings class")
    return found


def run_settings(args: argparse.Namespace) -> int:
    cls = load_class(args.target)
    if args.example:
        print(cls.example(args.prefix))
        return 0
    try:
        rows = cls.explain(prefix=args.prefix, dotenv=args.dotenv)
    except SettingsError as error:
        print(error, file=sys.stderr)
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
