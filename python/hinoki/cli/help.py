"""The ``hinoki`` argument parser: suggestions for mistyped commands and a
grouped command list in the top-level ``--help``."""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from collections.abc import Iterator
from typing import Any, NoReturn

from hinoki.terminal import Style

GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Start", ("new", "app", "dev", "serve", "shell")),
    (
        "Inspect",
        ("check", "doctor", "routes", "events", "settings", "openapi", "version"),
    ),
    ("Data", ("migrations", "admin")),
    ("Integrate", ("mcp", "skills", "manage", "completion")),
)
INVALID_CHOICE = re.compile(r"invalid choice: '([^']*)'")
FOOTER = "Run `hinoki COMMAND --help` for the options of one command."


def subparsers_of(parser: argparse.ArgumentParser) -> argparse._SubParsersAction[Any]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise LookupError("parser has no subcommands")


def command_help(parser: argparse.ArgumentParser) -> dict[str, str]:
    """Each subcommand's one-line help, in registration order."""
    return {
        action.dest: action.help or ""
        for action in subparsers_of(parser)._get_subactions()
    }


def grouped(commands: dict[str, str]) -> Iterator[tuple[str, list[str]]]:
    """The command names under their group; a command outside every group
    still appears, under ``Other``."""
    placed = {name for _, names in GROUPS for name in names}
    for title, names in GROUPS:
        yield title, [name for name in names if name in commands]
    yield "Other", [name for name in commands if name not in placed]


def help_text(style: Style, usage: str, commands: dict[str, str]) -> str:
    width = max(map(len, commands), default=0)
    lines = [usage.rstrip(), ""]
    for title, names in grouped(commands):
        if not names:
            continue
        lines.append(style.paint(title, "brand", bold=True))
        for name in names:
            label = style.paint(f"{name:<{width}}", "text", bold=True)
            lines.append(f"  {label}  {commands[name]}".rstrip())
        lines.append("")
    lines.append(style.dim(FOOTER))
    return "\n".join(lines) + "\n"


class HinokiParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, group_commands: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.group_commands = group_commands

    def suggestion(self, word: str) -> str | None:
        try:
            names = list(subparsers_of(self).choices)
        except LookupError:
            return None
        close = difflib.get_close_matches(word, names, n=1)
        return close[0] if close else None

    def error(self, message: str) -> NoReturn:
        found = INVALID_CHOICE.search(message)
        hint = self.suggestion(found.group(1)) if found else None
        if hint:
            message += f"\n  did you mean '{hint}'?"
        super().error(message)

    def format_help(self) -> str:
        if not self.group_commands:
            return super().format_help()
        return help_text(
            Style.detect(sys.stdout),
            self.format_usage(),
            command_help(self),
        )
