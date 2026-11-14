"""Dispatch ``hinoki manage TARGET COMMAND [OPTIONS]``."""

from __future__ import annotations

import argparse
import asyncio
import sys

from hinoki.checkcli import USAGE_ERRORS, build_app
from hinoki.errors import ShutdownError, StartupError
from hinoki.management import Command, CommandError
from hinoki.runtime.management import run_command


def _print_usage() -> None:
    print("usage: hinoki manage TARGET [COMMAND] [ARGS ...]")
    print("       hinoki manage TARGET --help")


def _print_commands(target: str, commands: tuple[Command, ...]) -> None:
    print(f"usage: hinoki manage {target} COMMAND [ARGS ...]")
    print("\nCommands:")
    if not commands:
        print("  (this app has no management commands)")
        return
    width = max(len(command.name) for command in commands)
    for command in commands:
        print(f"  {command.name:<{width}}  {command.help}")


def run(argv: list[str]) -> int:  # noqa: PLR0911 - each CLI branch has a distinct status
    """Load an app, list its commands or execute one command."""
    if not argv:
        _print_usage()
        return 2
    if argv[0] in {"-h", "--help"}:
        _print_usage()
        return 0

    target, *arguments = argv
    try:
        app = build_app(target)
    except (*USAGE_ERRORS, SystemExit) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not arguments or arguments[0] in {"-h", "--help"}:
        _print_commands(target, app.commands)
        return 0

    name, *command_arguments = arguments
    command = next((item for item in app.commands if item.name == name), None)
    if command is None:
        print(f"error: unknown management command {name!r}", file=sys.stderr)
        _print_commands(target, app.commands)
        return 2

    parser = argparse.ArgumentParser(
        prog=f"hinoki manage {target} {name}",
        description=command.help or None,
    )
    command.add_arguments(parser)
    args = parser.parse_args(command_arguments)
    try:
        return asyncio.run(run_command(app, command, args))
    except (CommandError, StartupError, ShutdownError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
