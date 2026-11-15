"""Dispatch ``hinoki manage TARGET COMMAND [OPTIONS]``."""

from __future__ import annotations

import argparse
import asyncio
import sys

from hinoki.checkcli import USAGE_ERRORS, build_app
from hinoki.errors import ShutdownError, StartupError
from hinoki.management import Command, CommandError
from hinoki.project import ProjectError, find_manifest_or_none, load_manifest
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
    if not argv or argv[0] in {"-h", "--help"}:
        if find_manifest_or_none() is not None:
            argv = ["--help"]
        else:
            _print_usage()
            return 2 if not argv else 0

    try:
        target, arguments = _resolve_target(argv)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
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


def _resolve_target(argv: list[str]) -> tuple[str, list[str]]:
    """Allow a project manifest to supply the default management target."""
    target, *arguments = argv
    if ":" in target:
        return target, arguments
    manifest_path = find_manifest_or_none()
    if manifest_path is None:
        return target, arguments
    try:
        manifest = load_manifest(manifest_path)
    except ProjectError:
        raise
    sys.path.insert(0, str(manifest.root))
    command_arguments = (
        arguments if target in {"-h", "--help"} else [target, *arguments]
    )
    return manifest.application, command_arguments
