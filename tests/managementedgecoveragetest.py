"""Management command registry and CLI error/help behavior."""

from __future__ import annotations

import argparse
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from hinoki import managementcli
from hinoki.errors import RegistrationError, ShutdownError, StartupError
from hinoki.management import Command, CommandContext, CommandError, _normalize_commands
from hinoki.modules import Module, ModuleContext
from hinoki.project import ProjectError, ProjectManifest


class ContextResource:
    pass


class ValidCommand(Command):
    name = "valid-command"
    help = "valid command help"

    def handle(self, _context: CommandContext, _args: Namespace) -> int:
        return 0


def test_command_base_context_and_registry_validation() -> None:
    with pytest.raises(NotImplementedError, match="handle is not implemented"):
        Command().handle(CommandContext(None, ModuleContext()), Namespace())

    context = ModuleContext()
    context.provide(ContextResource, ContextResource())
    command_context = CommandContext("settings", context)
    assert isinstance(command_context.get(ContextResource), ContextResource)
    assert isinstance(command_context.find(ContextResource), ContextResource)
    assert command_context.find(Module) is None
    with pytest.raises(LookupError, match="Module was not provided"):
        command_context.get(Module)

    with pytest.raises(RegistrationError, match="inherit from Command"):
        _normalize_commands([object()])  # type: ignore[list-item]
    assert _normalize_commands([ValidCommand()])[0].name == "valid-command"

    class NeedsArgument(Command):
        name = "needs-argument"

        def __init__(self, required: str) -> None:
            self.required = required

    with pytest.raises(RegistrationError, match="cannot construct"):
        _normalize_commands([NeedsArgument])

    class BadHelp(Command):
        name = "bad-help"
        help = 3  # type: ignore[assignment]

    with pytest.raises(RegistrationError, match="help must be text"):
        _normalize_commands([BadHelp])

    class BadRequires(Command):
        name = "bad-requires"
        requires = cast(Any, [Module])

    with pytest.raises(RegistrationError, match="tuple of Module classes"):
        _normalize_commands([BadRequires])

    class DuplicateRequires(Command):
        name = "duplicate-requires"
        requires = (Module, Module)

    with pytest.raises(RegistrationError, match="more than once"):
        _normalize_commands([DuplicateRequires])


def test_management_cli_usage_and_target_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(managementcli, "find_manifest_or_none", lambda: None)
    assert managementcli.run([]) == 2
    assert "usage: hinoki manage TARGET" in capsys.readouterr().out
    assert managementcli.run(["--help"]) == 0
    capsys.readouterr()
    assert managementcli._resolve_target(["shop", "list"]) == ("shop", ["list"])

    manifest_path = tmp_path / "hinoki.toml"
    manifest_path.touch()
    manifest = ProjectManifest(manifest_path, "shop.main:app", ())
    monkeypatch.setitem(
        managementcli.__dict__,
        "sys",
        SimpleNamespace(path=list(sys.path), stderr=sys.stderr),
    )
    monkeypatch.setattr(managementcli, "find_manifest_or_none", lambda: manifest_path)
    monkeypatch.setattr(managementcli, "load_manifest", lambda _path: manifest)
    assert managementcli._resolve_target(["migrate", "--dry-run"]) == (
        "shop.main:app",
        ["migrate", "--dry-run"],
    )
    assert str(tmp_path) in managementcli.__dict__["sys"].path
    assert managementcli._resolve_target(["--help"]) == ("shop.main:app", [])
    monkeypatch.setattr(
        managementcli, "build_app", lambda _target: SimpleNamespace(commands=())
    )
    assert managementcli.run([]) == 0
    assert "no management commands" in capsys.readouterr().out

    def invalid_manifest(_path: Path) -> Any:
        raise ProjectError("invalid project manifest")

    monkeypatch.setattr(managementcli, "load_manifest", invalid_manifest)
    with pytest.raises(ProjectError, match="invalid project manifest"):
        managementcli._resolve_target(["migrate"])
    assert managementcli.run(["migrate"]) == 2
    assert "invalid project manifest" in capsys.readouterr().err


def test_management_cli_handles_target_and_application_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(managementcli, "find_manifest_or_none", lambda: None)
    monkeypatch.setattr(
        managementcli,
        "build_app",
        lambda _target: (_ for _ in ()).throw(ValueError("bad target")),
    )
    assert managementcli.run(["shop:app"]) == 2
    assert "error: bad target" in capsys.readouterr().err

    def failed_app(_target: str) -> Any:
        raise SystemExit("application failed")

    monkeypatch.setattr(managementcli, "build_app", failed_app)
    assert managementcli.run(["shop:app"]) == 2
    assert "application failed" in capsys.readouterr().err

    monkeypatch.setattr(
        managementcli,
        "build_app",
        lambda _target: SimpleNamespace(commands=()),
    )
    assert managementcli.run(["shop:app", "--help"]) == 0
    assert "no management commands" in capsys.readouterr().out


def test_management_cli_lists_named_commands_and_reports_command_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    command = ValidCommand()
    app = SimpleNamespace(commands=(command,))
    monkeypatch.setattr(managementcli, "build_app", lambda _target: app)
    assert managementcli.run(["shop:app", "--help"]) == 0
    assert "valid-command  valid command help" in capsys.readouterr().out

    class ErrorCommand(Command):
        name = "error-command"

        async def handle(self, _context: CommandContext, _args: Namespace) -> int:
            raise CommandError("execution failed")

    for error in (
        CommandError("command error"),
        StartupError("module", ValueError("startup")),
        ShutdownError([ValueError("shutdown")]),
    ):

        async def fail(
            _app: Any, _command: Any, _args: Any, _error: Exception = error
        ) -> int:
            raise _error

        monkeypatch.setattr(managementcli, "run_command", fail)
        assert managementcli.run(["shop:app", "valid-command"]) == 1
        assert "error:" in capsys.readouterr().err

    error_app = SimpleNamespace(commands=(ErrorCommand(),))
    monkeypatch.setattr(managementcli, "build_app", lambda _target: error_app)
    monkeypatch.setattr(
        managementcli,
        "run_command",
        lambda *_args: ErrorCommand().handle(cast(Any, None), Namespace()),
    )
    assert managementcli.run(["shop:app", "error-command"]) == 1
    assert "execution failed" in capsys.readouterr().err


def test_management_cli_rejects_unknown_command_and_maps_parser_help(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        managementcli,
        "build_app",
        lambda _target: SimpleNamespace(commands=(ValidCommand(),)),
    )
    assert managementcli.run(["shop:app", "unknown"]) == 2
    captured = capsys.readouterr()
    assert "unknown management command" in captured.err
    assert "valid-command" in captured.out

    class Arguments(Command):
        name = "arguments"

        def add_arguments(self, parser: argparse.ArgumentParser) -> None:
            parser.add_argument("--needed", required=True)

    monkeypatch.setattr(
        managementcli,
        "build_app",
        lambda _target: SimpleNamespace(commands=(Arguments(),)),
    )
    with pytest.raises(SystemExit):
        managementcli.run(["shop:app", "arguments"])
