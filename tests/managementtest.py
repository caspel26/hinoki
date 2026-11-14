from __future__ import annotations

import asyncio
from argparse import ArgumentParser, Namespace
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from hinoki import Hinoki, managementcli
from hinoki.__main__ import main, parser
from hinoki.context import event_loop
from hinoki.errors import RegistrationError
from hinoki.management import Command, CommandContext, CommandError
from hinoki.modules import Module, ModuleContext
from hinoki.runtime.management import modules_for_command, run_command

events: list[str] = []


class Resource:
    value = "ready"


class BaseModule(Module):
    name = "test-base"

    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        events.append("base:start")
        ctx.provide(Resource, Resource())
        try:
            yield
        finally:
            events.append("base:stop")


class RequiredModule(Module):
    name = "test-required"
    requires = (BaseModule,)

    @asynccontextmanager
    async def lifespan(self, _ctx: ModuleContext) -> AsyncIterator[None]:
        events.append("required:start")
        try:
            yield
        finally:
            events.append("required:stop")


class UnusedModule(Module):
    name = "test-unused"

    @asynccontextmanager
    async def lifespan(self, _ctx: ModuleContext) -> AsyncIterator[None]:
        events.append("unused:start")
        try:
            yield
        finally:
            events.append("unused:stop")


class FailingShutdownModule(Module):
    name = "test-failing-shutdown"

    @asynccontextmanager
    async def lifespan(self, _ctx: ModuleContext) -> AsyncIterator[None]:
        yield
        raise RuntimeError("shutdown failed")


class ReadResource(Command):
    name = "read-resource"
    requires = (RequiredModule,)

    def handle(self, context: CommandContext, _args: Namespace) -> int:
        assert context.get(Resource).value == "ready"
        assert event_loop.get() is not None
        return 7


class AsyncCommand(Command):
    name = "async-command"

    async def handle(self, _context: CommandContext, _args: Namespace) -> int:
        assert asyncio.get_running_loop()
        return 0


class ArgumentCommand(Command):
    name = "argument-command"
    help = "print the selected value"

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("value")

    def handle(self, _context: CommandContext, args: Namespace) -> None:
        print(args.value)


class MissingModuleCommand(Command):
    name = "missing-module"
    requires = (RequiredModule,)


class FailingCommand(Command):
    name = "failing"
    requires = (RequiredModule,)

    def handle(self, _context: CommandContext, _args: Namespace) -> None:
        raise RuntimeError("command failed")


class InvalidExitCode(Command):
    name = "invalid-exit-code"

    def handle(self, _context: CommandContext, _args: Namespace) -> int:
        return 256


class FailingWithShutdownError(Command):
    name = "failing-with-shutdown-error"
    requires = (FailingShutdownModule,)

    def handle(self, _context: CommandContext, _args: Namespace) -> None:
        raise ValueError("handler failed")


@pytest.fixture(autouse=True)
def clear_events() -> None:
    events.clear()


def test_sync_command_uses_declared_module_closure_and_executor() -> None:
    app = Hinoki(
        modules=[BaseModule(), RequiredModule(), UnusedModule()],
        commands=[ReadResource],
    )

    result = asyncio.run(run_command(app, app.commands[0], Namespace()))

    assert result == 7
    assert events == [
        "base:start",
        "required:start",
        "required:stop",
        "base:stop",
    ]


def test_async_command_without_modules_does_not_start_lifespans() -> None:
    app = Hinoki(modules=[UnusedModule()], commands=[AsyncCommand])

    result = asyncio.run(run_command(app, app.commands[0], Namespace()))

    assert result == 0
    assert events == []


def test_failed_command_still_closes_required_modules() -> None:
    app = Hinoki(modules=[BaseModule(), RequiredModule()], commands=[FailingCommand])

    with pytest.raises(RuntimeError, match="command failed"):
        asyncio.run(run_command(app, app.commands[0], Namespace()))

    assert events == [
        "base:start",
        "required:start",
        "required:stop",
        "base:stop",
    ]


def test_handler_error_is_preserved_when_shutdown_also_fails() -> None:
    app = Hinoki(modules=[FailingShutdownModule()], commands=[FailingWithShutdownError])

    with pytest.raises(ValueError, match="handler failed") as raised:
        asyncio.run(run_command(app, app.commands[0], Namespace()))

    assert raised.value.__notes__ == ["shutdown finished with errors: RuntimeError"]


def test_command_rejects_out_of_range_exit_code() -> None:
    app = Hinoki(commands=[InvalidExitCode])

    with pytest.raises(CommandError, match="exit code from 0 to 255"):
        asyncio.run(run_command(app, app.commands[0], Namespace()))


def test_command_missing_required_module_fails_before_startup() -> None:
    app = Hinoki(commands=[MissingModuleCommand])

    with pytest.raises(CommandError, match="is not installed on this app"):
        modules_for_command(app, app.commands[0])
    assert events == []


def test_command_registry_rejects_invalid_and_duplicate_names() -> None:
    class Invalid(Command):
        name = "Not valid"

    class Duplicate(Command):
        name = "same"

    class OtherDuplicate(Command):
        name = "same"

    with pytest.raises(RegistrationError, match="invalid command name"):
        Hinoki(commands=[Invalid])
    with pytest.raises(RegistrationError, match="duplicate management command"):
        Hinoki(commands=[Duplicate, OtherDuplicate])


def test_command_cli_lists_commands_and_parses_arguments(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    app = Hinoki(commands=[ArgumentCommand])
    monkeypatch.setattr(managementcli, "build_app", lambda _target: app)

    assert managementcli.run(["shop.main:app", "--help"]) == 0
    assert "argument-command  print the selected value" in capsys.readouterr().out

    assert managementcli.run(["shop.main:app", "argument-command", "hello"]) == 0
    assert capsys.readouterr().out == "hello\n"


def test_command_cli_unknown_command_lists_available_commands(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        managementcli, "build_app", lambda _target: Hinoki(commands=[ArgumentCommand])
    )

    assert managementcli.run(["shop.main:app", "unknown"]) == 2
    captured = capsys.readouterr()
    assert "unknown management command 'unknown'" in captured.err
    assert "argument-command" in captured.out


def test_main_routes_manage_without_changing_other_parsers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        managementcli, "run", lambda args: 23 if args == ["target"] else 1
    )

    assert main(["manage", "target"]) == 23
    assert parser().parse_args(["dev", "module:app"]).group == "dev"
