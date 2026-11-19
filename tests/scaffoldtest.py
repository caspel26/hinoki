"""``python -m hinoki new`` and the explain_error MCP tool."""

import argparse
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from hinoki import newcli
from hinoki.__main__ import main
from hinoki.errorcatalog import CATALOG, explain
from hinoki.mcp import DevProject, Server
from hinoki.newcli import fill_from_prompts
from hinoki.rules import RuleSet
from hinoki.scaffold import ScaffoldError, create, files
from hinoki.terminal import Style
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
    "name",
    [
        "",
        "Shop",
        "my_shop",
        "my-shop",
        "1shop",
        "a b",
        "../x",
        "hinoki",
        "json",
        "asyncio",
    ],
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
            "shop.main:app",
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


def test_the_project_names_where_hinoki_comes_from(tmp_path: Path) -> None:
    git = create("one", tmp_path, "git+https://example.org/h.git@v1")
    assert (
        'hinoki = { git = "https://example.org/h.git", rev = "v1" }'
        in (git / "pyproject.toml").read_text()
    )
    ssh = files("two", "ssh://git@example.org/h.git")["pyproject.toml"]
    assert 'git = "ssh://git@example.org/h.git" }' in ssh
    here = Path(__file__).resolve().parents[1]
    path = files("three", str(here))["pyproject.toml"]
    assert f'hinoki = {{ path = "{here}", editable = true }}' in path
    with pytest.raises(ScaffoldError, match="not a Hinoki source checkout"):
        create("four", tmp_path, str(tmp_path))
    assert not (tmp_path / "four").exists()  # nothing written on a bad source


def test_scaffold_toml_escapes_checkout_paths_and_git_values(tmp_path: Path) -> None:
    checkout = tmp_path / 'hinoki "quoted"\\checkout'
    (checkout / "crates").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("[project]\n")

    path_project = create("pathapp", tmp_path, str(checkout))
    path_config = tomllib.loads((path_project / "pyproject.toml").read_text())
    assert path_config["tool"]["uv"]["sources"]["hinoki"]["path"] == str(checkout)

    git_project = create("gitapp", tmp_path, 'https://example.org/a"b.git@rev\\x')
    git_config = tomllib.loads((git_project / "pyproject.toml").read_text())
    assert git_config["tool"]["uv"]["sources"]["hinoki"] == {
        "git": 'https://example.org/a"b.git',
        "rev": "rev\\x",
    }


def test_the_project_has_a_readme_with_the_commands(tmp_path: Path) -> None:
    readme = (create("shop", tmp_path) / "README.md").read_text()
    assert "uv run hinoki dev shop.main:app" in readme and "uv run pytest" in readme
    assert "not published on PyPI" in readme


def test_an_empty_existing_directory_is_used(tmp_path: Path) -> None:
    (tmp_path / "shop").mkdir()
    assert (create("shop", tmp_path) / "pyproject.toml").is_file()


def answers(*values: str) -> Any:
    queue = iter(values)
    return lambda _prompt: next(queue)


def new_args(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "name": None,
        "dir": None,
        "hinoki": None,
        "database": None,
        "agent_files": None,
        "git": None,
        "install": None,
        "no_input": False,
    }
    return argparse.Namespace(**{**base, **overrides})


def test_prompts_fill_only_what_the_flags_left_unset(tmp_path: Path) -> None:
    args = new_args(name="shop", dir=str(tmp_path), database="sqlite")
    fill_from_prompts(args, answers("n", "", "n"))
    assert (args.name, args.database) == ("shop", "sqlite")
    assert (args.agent_files, args.git, args.install) == (False, True, False)


def test_prompts_repeat_until_the_answer_is_valid() -> None:
    args = new_args()
    fill_from_prompts(
        args,
        answers(
            "Bad-Name", "hinoki", "shop", "", "oracle", "sqlite", "maybe", "y", "", ""
        ),
    )
    assert (args.name, args.dir, args.database, args.agent_files) == (
        "shop",
        ".",
        "sqlite",
        True,
    )


def test_a_non_interactive_run_needs_a_name_and_keeps_old_defaults(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new", "--no-input", "--dir", str(tmp_path)]) == 2
    assert "name is required" in capsys.readouterr().err
    assert main(["new", "shop", "--no-input", "--dir", str(tmp_path)]) == 0
    target = tmp_path / "shop"
    assert (target / "AGENTS.md").exists()
    assert not (target / ".git").exists()
    assert "    database:" not in (target / "shop/settings.py").read_text()


def test_agent_files_can_be_left_out(tmp_path: Path) -> None:
    target = create("shop", tmp_path, agent_files=False)
    assert not any(
        (target / n).exists() for n in ("AGENTS.md", "CLAUDE.md", ".mcp.json")
    )
    assert (target / "hinoki-rules.toml").exists()


def test_a_database_choice_declares_the_setting(tmp_path: Path) -> None:
    sqlite = create("lite", tmp_path, database="sqlite")
    assert (
        "database: settings.DatabaseSettings"
        in (sqlite / "lite/settings.py").read_text()
    )
    assert not (sqlite / ".env.example").exists()
    pg = create("big", tmp_path, database="postgresql")
    assert (
        (pg / ".env.example").read_text().startswith("BIG_DATABASE__URL=postgresql://")
    )
    with pytest.raises(ScaffoldError):
        create("odd", tmp_path, database="oracle")


def test_git_and_install_run_in_the_new_project_and_failures_are_not_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:

    calls: list[tuple[list[str], Path]] = []

    def fake_run(command: list[str], cwd: Path, **_options: Any) -> Any:
        calls.append((command, cwd))
        return subprocess.CompletedProcess(command, 1 if "sync" in command else 0)

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("shutil.which", lambda tool: f"/bin/{tool}")
    code = main(
        ["new", "shop", "--no-input", "--dir", str(tmp_path), "--git", "--install"]
    )
    assert code == 0
    assert [call[0][1:] for call in calls] == [["init"], ["sync"]]
    assert all(call[1] == tmp_path / "shop" for call in calls)
    captured = capsys.readouterr()
    assert "warning" in captured.err
    assert "uv sync && uv run pytest" in captured.out


def test_a_missing_tool_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:

    monkeypatch.setattr("shutil.which", lambda _name: None)
    assert main(["new", "shop", "--no-input", "--dir", str(tmp_path), "--git"]) == 0
    assert "skipped: git is not installed" in capsys.readouterr().err


def test_styling_is_used_only_on_a_capable_terminal() -> None:
    class Tty:
        encoding = "utf-8"

        def isatty(self) -> bool:
            return True

    class Pipe(Tty):
        def isatty(self) -> bool:
            return False

    fancy = Style.detect(Tty(), {"TERM": "xterm"})  # type: ignore[arg-type]
    assert fancy.ok("done").startswith("  \x1b[32m\u2714")
    assert "\x1b[" in fancy.banner("new project")
    for stream, env in [
        (Pipe(), {}),
        (Tty(), {"NO_COLOR": "1"}),
        (Tty(), {"TERM": "dumb"}),
    ]:
        plain = Style.detect(stream, env)  # type: ignore[arg-type]
        text = plain.ok("done") + plain.warning("w") + plain.banner("t")
        assert "\x1b" not in text
        assert text.isascii()
    ascii_only = Style(color=True, unicode=False)
    assert "\u2714" not in ascii_only.ok("done")


def test_the_database_prompt_accepts_a_number_or_a_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = new_args(name="shop", dir=".", agent_files=True, git=False, install=False)
    fill_from_prompts(args, answers("7", "2"))
    assert args.database == "sqlite"
    listing = capsys.readouterr().out
    assert "1) none" in listing and "3) postgresql" in listing
    args = new_args(name="shop", dir=".", agent_files=True, git=False, install=False)
    fill_from_prompts(args, answers("postgresql"))
    assert args.database == "postgresql"


def test_cancelling_a_prompt_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:

    def interrupted(_prompt: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(newcli, "interactive", lambda _args: True)
    args = new_args(dir=str(tmp_path))
    assert newcli.run_new(args, interrupted) == 130
    assert "nothing was created" in capsys.readouterr().err
    assert not any(tmp_path.iterdir())
