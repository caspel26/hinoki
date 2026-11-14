"""App-defined commands for local project operations.

Commands are registered on ``Hinoki(commands=[...])`` and run with the
application's settings plus only their declared module lifecycles.
"""

from __future__ import annotations

import re
from argparse import ArgumentParser, Namespace
from collections.abc import Awaitable, Sequence
from typing import ClassVar, TypeVar

from hinoki.errors import RegistrationError
from hinoki.modules import Module, ModuleContext

CommandResult = int | None | Awaitable[int | None]
_COMMAND_NAME = re.compile(r"[a-z][a-z0-9-]*\Z")
T = TypeVar("T")

__all__ = ["Command", "CommandContext", "CommandError", "CommandResult"]


class Command:
    """A project command with explicit arguments and module requirements.

    Set ``name`` and optionally ``help``/``requires`` on a subclass. Override
    ``add_arguments`` to define CLI arguments and ``handle`` to execute the
    command. A synchronous or asynchronous ``handle`` is supported.
    """

    name: ClassVar[str] = ""
    help: ClassVar[str] = ""
    requires: ClassVar[tuple[type[Module], ...]] = ()

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Add command-specific arguments to the parser."""

    def handle(self, context: CommandContext, args: Namespace) -> CommandResult:
        """Run the command; return an optional process exit code."""
        raise NotImplementedError(f"{type(self).__name__}.handle is not implemented")


class CommandError(Exception):
    """The command could not start or returned an invalid result."""


class CommandContext:
    """Settings and resources available during one command invocation."""

    def __init__(self, settings: object | None, modules: ModuleContext) -> None:
        self.settings = settings
        self._modules = modules

    def get(self, kind: type[T]) -> T:
        """Return a module resource or raise ``LookupError`` if unavailable."""
        return self._modules.get(kind)

    def find(self, kind: type[T]) -> T | None:
        """Return a module resource, or ``None`` when it is unavailable."""
        return self._modules.find(kind)


def _normalize_commands(
    commands: Sequence[Command | type[Command]],
) -> tuple[Command, ...]:
    """Construct command classes and validate the app's command registry."""
    normalized: list[Command] = []
    names: set[str] = set()
    for declaration in commands:
        command = _command_instance(declaration)
        _validate_command(command)
        if command.name in names:
            raise RegistrationError(f"duplicate management command {command.name!r}")
        names.add(command.name)
        normalized.append(command)
    return tuple(normalized)


def _command_instance(declaration: Command | type[Command]) -> Command:
    if isinstance(declaration, Command):
        return declaration
    if not isinstance(declaration, type) or not issubclass(declaration, Command):
        raise RegistrationError("management commands must inherit from Command")
    try:
        return declaration()
    except TypeError as exc:
        raise RegistrationError(
            f"cannot construct management command {declaration.__name__}: {exc}"
        ) from None


def _validate_command(command: Command) -> None:
    if not isinstance(command.name, str) or not _COMMAND_NAME.fullmatch(command.name):
        raise RegistrationError(
            f"invalid command name {command.name!r}; use lower-case letters, "
            "digits and hyphens, starting with a letter"
        )
    if not isinstance(command.help, str):
        raise RegistrationError(f"command {command.name!r} help must be text")
    if not isinstance(command.requires, tuple) or any(
        not isinstance(module, type) or not issubclass(module, Module)
        for module in command.requires
    ):
        raise RegistrationError(
            f"command {command.name!r} requires must be a tuple of Module classes"
        )
    if len(set(command.requires)) != len(command.requires):
        raise RegistrationError(
            f"command {command.name!r} lists a module requirement more than once"
        )
