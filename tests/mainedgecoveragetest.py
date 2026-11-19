"""Command dispatcher paths that are hard to reach through integration flows."""

from __future__ import annotations

import runpy
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from hinoki import __main__ as cli
from hinoki.ai import mcp
from hinoki.errors import MigrationError
from hinoki.orm.migrations import Plan


def test_legacy_migration_plan_wrapper_and_missing_models(tmp_path: Path) -> None:
    args = cli.parser().parse_args(
        [
            "migrations",
            "--dir",
            str(tmp_path),
            "plan",
            "--models",
            "blog.models",
            "--dry-run",
        ]
    )
    assert cli.migration_plan(args).operations

    missing = cli.parser().parse_args(["migrations", "plan"])
    with pytest.raises(MigrationError, match="pass --models"):
        cli.migration_plans(missing)


def test_all_app_migration_summary_reports_an_empty_plan(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        cli.print_all_app_plans(Namespace(check=False), [("accounts", Plan((), {}))])
        == 0
    )
    assert (
        capsys.readouterr().out
        == "accounts:\n  no changes\nno changes across registered apps\n"
    )


def test_project_sql_dispatch_uses_the_project_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = object()
    calls: list[tuple[object, str]] = []
    monkeypatch.setattr(cli, "manifest_for", lambda _args: manifest)
    monkeypatch.setattr(
        cli,
        "sql_project",
        lambda args, found: calls.append((found, args.name)),
    )

    assert cli.main(["migrations", "sql", "0001-initial"]) == 0
    assert calls == [(manifest, "0001-initial")]


def test_mcp_dispatch_loads_its_optional_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp, "run_mcp", lambda _args: 17)
    assert cli.run_mcp_command(Namespace()) == 17


def test_module_entrypoint_exits_with_the_command_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ScriptExit(BaseException):
        def __init__(self, code: int) -> None:
            self.code = code

    def exit_with(code: int) -> None:
        raise ScriptExit(code)

    monkeypatch.setattr(sys, "argv", ["hinoki", "version"])
    monkeypatch.setattr(sys, "exit", exit_with)
    with pytest.raises(ScriptExit) as raised:
        runpy.run_path(str(Path(cli.__file__)), run_name="__main__")
    assert raised.value.code == 0
