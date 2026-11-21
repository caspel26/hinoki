"""Generate and check an application's OpenAPI document without serving it."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from hinoki import jsontext
from hinoki.ai.openapidiff import breaking_changes
from hinoki.checkcli import USAGE_ERRORS, build_app


def add_arguments(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    command = commands.add_parser(
        "openapi", help="print, write or check an application's OpenAPI document"
    )
    command.add_argument(
        "target", nargs="?", help="module:app or module:factory (default: project app)"
    )
    output = command.add_mutually_exclusive_group()
    output.add_argument("--output", type=Path, help="write the document to this file")
    output.add_argument(
        "--check", type=Path, help="compare the document with this JSON file"
    )
    output.add_argument(
        "--breaking-against",
        type=Path,
        metavar="BASELINE",
        help="exit 1 when the document breaks clients of this earlier JSON file",
    )


def document(target: str) -> dict[str, Any]:
    """Build an app using the same target rules as ``hinoki check``."""
    return build_app(target).openapi()


def write_document(path: Path, rendered: str) -> int:
    try:
        path.write_text(rendered, encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot write {path}: {exc}", file=sys.stderr)
        return 2
    return 0


def check_document(path: Path, target: str, generated: dict[str, Any]) -> int:
    try:
        existing = jsontext.loads(path.read_bytes())
    except FileNotFoundError:
        print(f"error: OpenAPI file {path} does not exist", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"error: cannot read JSON from {path}: {exc}", file=sys.stderr)
        return 2
    if existing != generated:
        print(
            f"error: {path} is out of date; run hinoki openapi "
            f"{target} --output {path}",
            file=sys.stderr,
        )
        return 1
    return 0


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = jsontext.loads(path.read_bytes())
    except FileNotFoundError:
        print(f"error: OpenAPI file {path} does not exist", file=sys.stderr)
        return None
    except (OSError, ValueError) as exc:
        print(f"error: cannot read JSON from {path}: {exc}", file=sys.stderr)
        return None
    if not isinstance(loaded, dict):
        print(f"error: {path} is not an OpenAPI object", file=sys.stderr)
        return None
    return loaded


def check_breaking(path: Path, generated: dict[str, Any]) -> int:
    baseline = load_json(path)
    if baseline is None:
        return 2
    problems = breaking_changes(baseline, generated)
    for problem in problems:
        print(f"breaking: {problem}", file=sys.stderr)
    return 1 if problems else 0


def emit(args: argparse.Namespace, generated: dict[str, Any]) -> int:
    rendered = jsontext.dumps(generated, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        return write_document(args.output, rendered)
    if args.check is not None:
        return check_document(args.check, args.target, generated)
    if args.breaking_against is not None:
        return check_breaking(args.breaking_against, generated)
    print(rendered, end="")
    return 0


def run_openapi(args: argparse.Namespace) -> int:
    try:
        generated = document(args.target)
    except (*USAGE_ERRORS, SystemExit) as exc:
        print(f"error: cannot load {args.target}: {exc}", file=sys.stderr)
        return 2
    return emit(args, generated)
