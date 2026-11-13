"""``python -m hinoki new`` and the explain_error MCP tool."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from hinoki.__main__ import main
from hinoki.errorcatalog import CATALOG, explain
from hinoki.mcp import DevProject, Server
from hinoki.rules import RuleSet
from hinoki.scaffold import ScaffoldError, create, files
from tests.projects.rulesapp import create_app, rules


def test_the_project_has_what_an_agent_needs(tmp_path: Path) -> None:
    target = create("shop", tmp_path)
    names = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
    assert {
        "AGENTS.md",
        "CLAUDE.md",
        ".mcp.json",
        "hinoki-rules.toml",
        "pyproject.toml",
        "shop/main.py",
        "shop/rules.py",
        "shop/api.py",
        "tests/healthtest.py",
    } <= names
    assert not any("_" in Path(n).name.replace("__init__.py", "") for n in names)
    agents = (target / "AGENTS.md").read_text()
    assert "must not edit `hinoki-rules.toml`" in agents.replace(
        "Do not edit `hinoki-rules.toml`", "must not edit `hinoki-rules.toml`"
    )
    assert (
        "HNK-ACC-001" in agents
        and "request: Request" in agents
        and "explain_error" in agents
    )
    mcp = json.loads((target / ".mcp.json").read_text())
    assert mcp["mcpServers"]["hinoki"]["args"][-2:] == ["--package", "shop"]
    assert "read and follow" in (target / "CLAUDE.md").read_text().lower()


@pytest.mark.parametrize(
    "name", ["", "Shop", "my_shop", "my-shop", "1shop", "a b", "../x"]
)
def test_bad_project_names_are_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(ScaffoldError, match="name must be"):
        create(name, tmp_path)


def test_an_existing_non_empty_directory_is_not_overwritten(tmp_path: Path) -> None:
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "keep.txt").write_text("mine")
    with pytest.raises(ScaffoldError, match="not empty"):
        create("shop", tmp_path)
    assert (tmp_path / "shop" / "keep.txt").read_text() == "mine"
    (tmp_path / "empty").mkdir()
    assert create("empty", tmp_path).name == "empty"


def test_the_command_reports_success_and_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new", "shop", "--dir", str(tmp_path)]) == 0
    assert "created" in capsys.readouterr().out
    assert main(["new", "shop", "--dir", str(tmp_path)]) == 2
    assert "error:" in capsys.readouterr().err


def test_the_generated_project_passes_its_own_rules_and_tests(tmp_path: Path) -> None:
    target = create("shop", tmp_path)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(target), *sys.path])}
    check = subprocess.run(
        [
            sys.executable,
            "-m",
            "hinoki",
            "check",
            "--app",
            "shop.main:create_app",
            "--rules",
            "shop.rules:project_rules",
            "--package",
            "shop",
        ],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert check.returncode == 0, check.stdout + check.stderr
    tests = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/healthtest.py",
            "--rootdir",
            str(target),
            "-c",
            str(target / "pyproject.toml"),
        ],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert tests.returncode == 0, tests.stdout + tests.stderr


def test_every_generated_file_is_text_with_a_trailing_newline() -> None:
    for relative, content in files("shop").items():
        assert content == "" or content.endswith("\n"), relative


def call(server: Server, **arguments: Any) -> dict[str, Any]:
    answer = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "explain_error", "arguments": arguments},
        }
    )
    assert answer is not None
    return answer["result"]  # type: ignore[no-any-return]


def test_explain_error_knows_codes_rules_and_says_so_when_it_does_not(
    tmp_path: Path,
) -> None:
    server = Server(DevProject(create_app(), rules, waivers=tmp_path / "w.toml"))
    code = call(server, code="extra_forbidden")["structuredContent"]
    assert code["kind"] == "error" and "schema does not declare" in code["meaning"]
    rule = call(server, code="HNK-ACC-001")["structuredContent"]
    assert rule["kind"] == "rule" and "do not edit waivers" in rule["fix"]
    missing = call(server, code="min_len")
    assert missing["isError"] and "min_length" in missing["structuredContent"]["error"]
    assert call(server)["isError"]
    assert call(server, code="zzz")["isError"]
    assert RuleSet().rules == ()


def test_the_catalogue_covers_what_the_framework_emits() -> None:
    for code in (
        "validation_error",
        "required",
        "type",
        "unsupported_media_type",
        "not_acceptable",
        "invalid_json",
        "overloaded",
        "timeout",
    ):
        assert explain(code) is not None
    assert all(meaning and fix for meaning, fix in CATALOG.values())
