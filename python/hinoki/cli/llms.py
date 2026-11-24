"""Render and check an application's llms.txt text without serving it."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from hinoki.ai.llms import render_llms
from hinoki.cli.check import USAGE_ERRORS, build_app
from hinoki.cli.openapi import write_document


def add_arguments(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    command = commands.add_parser(
        "llms", help="print, write or check an application's llms.txt for agents"
    )
    command.add_argument(
        "target", nargs="?", help="module:app or module:factory (default: project app)"
    )
    output = command.add_mutually_exclusive_group()
    output.add_argument("--output", type=Path, help="write the text to this file")
    output.add_argument(
        "--check", type=Path, help="compare the text with this Markdown file"
    )


def check_text(path: Path, target: str, rendered: str) -> int:
    try:
        existing = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    except FileNotFoundError:
        print(f"error: llms file {path} does not exist", file=sys.stderr)
        return 2
    except (OSError, UnicodeDecodeError) as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        return 2
    if existing != rendered:
        print(
            f"error: {path} is out of date; run hinoki llms {target} --output {path}",
            file=sys.stderr,
        )
        return 1
    return 0


def run_llms(args: argparse.Namespace) -> int:
    try:
        app = build_app(args.target)
        rendered = render_llms(app.openapi(), openapi_url=app.openapi_url)
    except (*USAGE_ERRORS, SystemExit) as exc:
        print(f"error: cannot load {args.target}: {exc}", file=sys.stderr)
        return 2
    if args.output is not None:
        return write_document(args.output, rendered)
    if args.check is not None:
        return check_text(args.check, args.target, rendered)
    print(rendered, end="")
    return 0
