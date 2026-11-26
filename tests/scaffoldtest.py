"""``python -m hinoki new`` and the explain_error MCP tool."""

import argparse
import contextlib
import io
import itertools
import json
import os
import pty
import re
import subprocess
import sys
import termios
import threading
import tomllib
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hinoki import terminal
from hinoki.__main__ import main
from hinoki.ai.errorcatalog import CATALOG, explain
from hinoki.ai.mcp import DevProject, Server
from hinoki.cli import appprompts
from hinoki.cli import new as newcli
from hinoki.cli.appprompts import name_check, report_app, requires_check
from hinoki.cli.new import fill_from_prompts
from hinoki.cli.project import create_app as create_project_app
from hinoki.cli.prompts import (
    Choice,
    Confirm,
    Prompter,
    Text,
    keys_available,
    raw_keys,
    read_keys,
    tokens,
)
from hinoki.project import ProjectError
from hinoki.rules import RuleSet
from hinoki.scaffold import ScaffoldError, create, files
from hinoki.terminal import (
    ANSI,
    CLEAR_LINE,
    RESET,
    TAGLINE,
    Style,
    elapsed,
    fit,
    shown_path,
)
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
    assert "https://hinoki.caspel.dev/llms.txt" in agents
    assert "llms-full.txt" in agents
    mcp = json.loads((target / ".mcp.json").read_text())
    assert mcp["mcpServers"]["hinoki"]["args"] == ["run", "hinoki", "mcp"]
    assert "read and follow" in (target / "CLAUDE.md").read_text().lower()


def test_the_project_root_owns_no_models_and_registers_no_app(tmp_path: Path) -> None:
    target = create("shop", tmp_path)
    manifest = tomllib.loads((target / "hinoki.toml").read_text())
    assert "apps" not in manifest
    assert not (target / "shop/models.py").exists()
    assert not (target / "shop/migrations").exists()


def test_underscores_are_allowed_between_words_in_project_and_app_names(
    tmp_path: Path,
) -> None:
    target = create("my_shop", tmp_path)
    created = create_project_app("api_v1", target)
    assert created == target / "my_shop/apps/api_v1"
    manifest = tomllib.loads((target / "hinoki.toml").read_text())
    assert [app["models"] for app in manifest["apps"]] == ["my_shop.apps.api_v1.models"]


def test_models_live_in_apps_created_after_the_project(tmp_path: Path) -> None:
    target = create("shop", tmp_path)
    created = create_project_app("catalog", target)
    assert created == target / "shop/apps/catalog"
    assert (created / "models.py").is_file()
    manifest = tomllib.loads((target / "hinoki.toml").read_text())
    assert [app["models"] for app in manifest["apps"]] == ["shop.apps.catalog.models"]


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Shop",
        "my__shop",
        "shop_",
        "_shop",
        "class",
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
    assert "include=" not in (target / "shop/main.py").read_text()
    assert "manifest_routes" not in (target / "shop/main.py").read_text()
    assert "rules=" not in (target / "shop/main.py").read_text()
    manifest = tomllib.loads((target / "hinoki.toml").read_text())
    assert manifest["project"]["rules"] == ["shop.rules:project_rules"]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(target), *sys.path])}
    check = subprocess.run(
        [sys.executable, "-m", "hinoki", "check"],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert check.returncode == 0, check.stdout + check.stderr
    assert "no rules were run" not in check.stderr
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
        'hinoki-api = { git = "https://example.org/h.git", rev = "v1" }'
        in (git / "pyproject.toml").read_text()
    )
    ssh = files("two", "ssh://git@example.org/h.git")["pyproject.toml"]
    assert 'git = "ssh://git@example.org/h.git" }' in ssh
    here = Path(__file__).resolve().parents[1]
    path = files("three", str(here))["pyproject.toml"]
    assert f'hinoki-api = {{ path = "{here}", editable = true }}' in path
    with pytest.raises(ScaffoldError, match="not a Hinoki source checkout"):
        create("four", tmp_path, str(tmp_path))
    assert not (tmp_path / "four").exists()  # nothing written on a bad source


def test_scaffold_toml_escapes_checkout_paths_and_git_values(tmp_path: Path) -> None:
    checkout = tmp_path / 'hinoki "quoted"\\checkout'
    (checkout / "crates").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("[project]\n")

    path_project = create("pathapp", tmp_path, str(checkout))
    path_config = tomllib.loads((path_project / "pyproject.toml").read_text())
    assert path_config["tool"]["uv"]["sources"]["hinoki-api"]["path"] == str(checkout)

    git_project = create("gitapp", tmp_path, 'https://example.org/a"b.git@rev\\x')
    git_config = tomllib.loads((git_project / "pyproject.toml").read_text())
    assert git_config["tool"]["uv"]["sources"]["hinoki-api"] == {
        "git": 'https://example.org/a"b.git',
        "rev": "rev\\x",
    }


def test_the_project_has_a_readme_with_the_commands(tmp_path: Path) -> None:
    readme = (create("shop", tmp_path) / "README.md").read_text()
    assert "uv run hinoki dev " in readme and "uv run pytest" in readme
    assert "main:app" not in readme
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
        "server": None,
        "agent_files": None,
        "skills": None,
        "git": None,
        "install": None,
        "app": None,
        "no_app": False,
        "no_input": False,
    }
    return argparse.Namespace(**{**base, **overrides})


def test_prompts_fill_only_what_the_flags_left_unset(tmp_path: Path) -> None:
    args = new_args(name="shop", dir=str(tmp_path), database="sqlite")
    fill_from_prompts(args, answers("", "n", "", "", "n", "n"))
    assert (args.name, args.database, args.server) == ("shop", "sqlite", "kiso")
    assert (args.agent_files, args.skills, args.git, args.install) == (
        False,
        False,
        True,
        False,
    )


def test_the_skills_question_can_be_answered_yes() -> None:
    args = new_args(
        name="shop",
        dir=".",
        database="none",
        server="kiso",
        agent_files=True,
        git=False,
        install=False,
    )
    fill_from_prompts(args, answers("y", "n"))
    assert args.skills is True


def test_prompts_repeat_until_the_answer_is_valid() -> None:
    args = new_args()
    fill_from_prompts(
        args,
        answers(
            "Bad-Name",
            "hinoki",
            "shop",
            "",
            "oracle",
            "sqlite",
            "nginx",
            "granian",
            "maybe",
            "y",
            "",
            "",
            "",
            "n",
        ),
    )
    assert (args.name, args.dir, args.database, args.server, args.agent_files) == (
        "shop",
        ".",
        "sqlite",
        "granian",
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
    assert "after you add: database:" in (target / "shop/settings.py").read_text()


def test_agent_files_can_be_left_out(tmp_path: Path) -> None:
    target = create("shop", tmp_path, agent_files=False)
    assert not any(
        (target / n).exists() for n in ("AGENTS.md", "CLAUDE.md", ".mcp.json")
    )
    assert (target / "hinoki-rules.toml").exists()


def test_a_database_choice_declares_the_setting(tmp_path: Path) -> None:
    sqlite = create("lite", tmp_path, database="sqlite")
    settings_text = (sqlite / "lite/settings.py").read_text()
    assert "database: settings.DatabaseSettings" in settings_text
    assert "after you add" not in settings_text
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
    assert fancy.ok("done").startswith("  \x1b[")
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


FACTS = [("python", "3.14.0"), ("folder", "~/code"), ("docs", "hinoki.caspel.dev")]


def branch_lines(text: str) -> list[str]:
    return [line for line in ANSI.sub("", text).split("\n") if line.strip()]


@pytest.mark.parametrize("columns", [60, 80, 120])
def test_the_banner_is_a_branch_with_the_title_the_tagline_and_the_facts(
    monkeypatch: pytest.MonkeyPatch, columns: int
) -> None:
    class Tty:
        encoding = "utf-8"

        def isatty(self) -> bool:
            return True

    monkeypatch.setenv("COLUMNS", str(columns))
    style = Style.detect(Tty(), {"TERM": "xterm-256color"})  # type: ignore[arg-type]

    assert branch_lines(style.banner("new project", "v1.2.3", FACTS)) == [
        "  \u256d\u2500 Hinoki  Python API framework  v1.2.3",
        "  \u2502",
        "  \u2502  python  3.14.0",
        "  \u2502  folder  ~/code",
        "  \u2502  docs    hinoki.caspel.dev",
        "  \u2570\u2500",
    ]


def test_the_ascii_branch_names_the_command_without_unicode() -> None:
    style = Style(color=True, unicode=False, width=80)
    text = style.banner("new project", "v1.2.3", FACTS)

    assert text.isascii()
    assert branch_lines(text)[0] == (
        "  +- Hinoki  Python API framework  new project - v1.2.3"
    )
    assert branch_lines(text)[-1] == "  +-"


def test_a_narrow_terminal_drops_the_tagline_under_the_title_and_wraps_values() -> None:
    style = Style(color=True, unicode=True, width=40)
    lines = branch_lines(
        style.banner("new project", "v1.2.3", [("docs", "a long value " * 4)])
    )

    assert lines[:2] == [
        "  \u256d\u2500 Hinoki  v1.2.3",
        "  \u2502  Python API framework",
    ]
    assert all(len(line) <= 39 for line in lines)
    assert sum(line.startswith("  \u2502  docs") for line in lines) == 1


def test_a_word_too_long_for_its_row_loses_its_start() -> None:
    style = Style(color=True, unicode=True, width=40)
    text = ANSI.sub("", style.banner("t", "v1", [("path", "~/" + "x" * 60)]))

    assert "\u2026" + "x" * 20 in text and "x" * 40 not in text


@pytest.mark.parametrize(
    ("style", "title"),
    [
        (
            Style(color=True, unicode=True, depth="truecolor"),
            "\x1b[1;38;2;238;237;242m",
        ),
        (Style(color=True, unicode=True, depth="256"), "\x1b[1;38;5;"),
        (Style(color=True, unicode=True, depth="16", light=True), "\x1b[1;39mHinoki"),
    ],
)
def test_the_title_is_bold_primary_text_at_every_depth(
    style: Style, title: str
) -> None:
    assert title in style.banner("new project", "v1.2.3", FACTS)


@pytest.mark.parametrize(
    "env",
    [
        {"TERM_PROGRAM": "ghostty", "TERM": "xterm-256color"},
        {"TERM_PROGRAM": "iTerm.app", "TERM": "xterm-256color"},
        {"KITTY_WINDOW_ID": "1", "TERM": "xterm-kitty"},
    ],
)
def test_image_terminals_get_the_same_branch_as_any_other(env: dict[str, str]) -> None:
    class Tty(io.StringIO):
        encoding = "utf-8"

        def isatty(self) -> bool:
            return True

    text = Style.detect(Tty(), env).banner("new project", "v1.2.3")

    assert "\u256d" in text
    assert "\x1b_G" not in text and "\x1b]1337" not in text
    assert not hasattr(Style(), "images")


def test_the_plain_banner_stays_one_line() -> None:
    assert Style().banner("new project", "v1.2.3") == "hinoki v1.2.3 - new project"


def test_the_shimmer_settles_on_exactly_the_static_banner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    style = Style(color=True, unicode=True, depth="truecolor", width=72)
    out = io.StringIO()
    style.shimmer("new project", "v1.2.3", FACTS, out)

    frames = out.getvalue()
    assert frames.count("\r") > len("Python API framework")
    static = style.banner("new project", "v1.2.3", FACTS)
    assert re.sub(r"[^\n]*\r", "", frames) == static + "\n"


def test_the_guide_rail_opens_with_the_branded_prefix() -> None:
    style = Style(color=True, unicode=True)
    head = ANSI.sub("", style.intro("new project")).strip().split("\n")[0]

    assert head == "\u250c  hinoki \u203a new project  esc to cancel"


def test_the_server_choice_is_kiso_unless_the_project_asks_for_granian(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kiso = create("fast", tmp_path)
    assert not (kiso / "settings.toml").exists()
    assert "toml=" not in (kiso / "fast/settings.py").read_text()
    granian = create("calm", tmp_path, server="granian", database="sqlite")
    assert (granian / "settings.toml").read_text().count('server = "granian"') == 1
    monkeypatch.chdir(granian)
    namespace: dict[str, Any] = {}
    exec((granian / "calm/settings.py").read_text(), namespace)  # noqa: S102
    loaded = namespace["ProjectSettings"].load()
    assert loaded.server.server == "granian"
    monkeypatch.setenv("CALM_SERVER__SERVER", "kiso")
    assert namespace["ProjectSettings"].load().server.server == "kiso"


def test_an_unknown_server_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ScaffoldError, match="server must be one of kiso, granian"):
        create("shop", tmp_path, server="uvicorn")
    with pytest.raises(SystemExit):
        main(
            ["new", "shop", "--no-input", "--server", "uvicorn", "--dir", str(tmp_path)]
        )
    assert (
        main(
            ["new", "shop", "--no-input", "--server", "granian", "--dir", str(tmp_path)]
        )
        == 0
    )
    assert (tmp_path / "shop/settings.toml").is_file()


def test_the_database_prompt_accepts_a_number_or_a_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = new_args(
        name="shop",
        dir=".",
        server="kiso",
        agent_files=True,
        skills=False,
        git=False,
        install=False,
    )
    fill_from_prompts(args, answers("7", "2", "n"))
    assert args.database == "sqlite"
    listing = capsys.readouterr().out
    assert "1) none" in listing and "3) postgresql" in listing
    args = new_args(
        name="shop",
        dir=".",
        server="kiso",
        agent_files=True,
        skills=False,
        git=False,
        install=False,
    )
    fill_from_prompts(args, answers("postgresql", "n"))
    assert args.database == "postgresql"


def keyed(text: str) -> tuple[Prompter, io.StringIO]:
    out = io.StringIO()
    style = Style(color=True, unicode=True, depth="truecolor")
    return Prompter(style, tokens(text), out), out


def test_key_driven_prompts_settle_into_a_summary_line() -> None:
    prompter, out = keyed(
        "Bad\r\x7f\x7f\x7fshop\r\r\x1b[B\x1b[B\x1b[A\r\x1b[B\rn\r\r\r\rn\r"
    )
    args = new_args()
    newcli.fill(args, prompter)
    assert (args.name, args.dir, args.database, args.server) == (
        "shop",
        ".",
        "sqlite",
        "granian",
    )
    assert (args.agent_files, args.skills, args.git, args.install) == (
        False,
        False,
        True,
        True,
    )
    shown = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", out.getvalue())
    assert "use lower-case letters, digits and single underscores" in shown
    assert "\u25c7  Name" in shown and "sqlite" in shown


def test_escape_cancels_a_key_driven_prompt() -> None:
    prompter, out = keyed("sh\x1b")
    with pytest.raises(KeyboardInterrupt):
        newcli.fill(new_args(), prompter)
    assert "\u25a0  Name" in re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", out.getvalue())


def test_a_light_terminal_background_gets_the_light_palette() -> None:
    class Tty:
        encoding = "utf-8"

        def isatty(self) -> bool:
            return True

    env = {"TERM": "xterm", "COLORTERM": "truecolor"}
    dark = Style.detect(Tty(), env)  # type: ignore[arg-type]
    light = Style.detect(Tty(), {**env, "COLORFGBG": "0;15"})  # type: ignore[arg-type]
    assert dark.text("x") == "\x1b[38;2;238;237;242mx\x1b[0m"
    assert light.text("x") == "\x1b[38;2;29;26;36mx\x1b[0m"


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


def registered_apps(project: Path) -> list[str]:
    manifest = tomllib.loads((project / "hinoki.toml").read_text())
    return [app["name"] for app in manifest.get("apps", [])]


def fill_app_question(*values: str, **overrides: Any) -> Any:
    args = new_args(
        name="shop",
        dir=".",
        database="none",
        server="kiso",
        agent_files=True,
        skills=False,
        git=False,
        install=False,
        **overrides,
    )
    fill_from_prompts(args, answers(*values))
    return args


def test_the_last_question_offers_an_app_and_declining_asks_no_name() -> None:
    assert fill_app_question("n").app is None
    assert fill_app_question("y", "").app == newcli.DEFAULT_APP


def test_the_app_name_is_asked_again_until_it_is_valid(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert fill_app_question("y", "my-posts", "json", "Posts", "posts").app == "posts"
    assert capsys.readouterr().err.count("single underscores") == 3


def test_app_flags_skip_the_question() -> None:
    assert fill_app_question(app="posts").app == "posts"
    assert fill_app_question(no_app=True).app is None


def test_a_terminal_run_creates_the_app_named_at_the_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(newcli, "interactive", lambda _args: True)
    args = new_args(dir=str(tmp_path))
    prompts = answers("shop", "", "", "n", "n", "n", "n", "y", "posts")
    assert newcli.run_new(args, prompts) == 0
    project = tmp_path / "shop"
    assert registered_apps(project) == ["posts"]
    assert (project / "shop/apps/posts/models.py").is_file()
    out = capsys.readouterr().out
    assert "created app posts" in out and "app create" not in out


def test_declining_the_prompt_keeps_a_project_without_apps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(newcli, "interactive", lambda _args: True)
    args = new_args(dir=str(tmp_path))
    prompts = answers("shop", "", "", "n", "n", "n", "n", "n")
    assert newcli.run_new(args, prompts) == 0
    assert registered_apps(tmp_path / "shop") == []
    assert "hinoki app create" in capsys.readouterr().out


def test_end_of_input_at_the_app_question_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(newcli, "interactive", lambda _args: True)
    queue = iter(["shop", "", "", "n", "n", "n", "n"])

    def ask(_prompt: str) -> str:
        try:
            return next(queue)
        except StopIteration:
            raise EOFError from None

    assert newcli.run_new(new_args(dir=str(tmp_path)), ask) == 130
    assert "nothing was created" in capsys.readouterr().err
    assert not any(tmp_path.iterdir())


def test_without_a_terminal_nothing_is_asked_and_no_app_is_created(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new", "shop", "--dir", str(tmp_path)]) == 0
    assert registered_apps(tmp_path / "shop") == []
    assert "Create an app" not in capsys.readouterr().out


def test_the_app_flag_creates_the_app_without_asking(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new", "shop", "--app", "posts", "--dir", str(tmp_path)]) == 0
    assert registered_apps(tmp_path / "shop") == ["posts"]
    assert "created app posts" in capsys.readouterr().out


def test_the_no_app_flag_creates_no_app(tmp_path: Path) -> None:
    assert main(["new", "shop", "--no-app", "--dir", str(tmp_path)]) == 0
    assert registered_apps(tmp_path / "shop") == []


def test_a_bad_app_flag_is_refused_before_anything_is_created(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new", "shop", "--app", "my-posts", "--dir", str(tmp_path)]) == 2
    assert "single underscores" in capsys.readouterr().err
    assert not any(tmp_path.iterdir())


def test_the_app_flags_exclude_each_other(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["new", "shop", "--app", "posts", "--no-app", "--dir", str(tmp_path)])


FANCY = Style(color=True, unicode=True, depth="truecolor")


def plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)


def test_names_are_checked_before_an_app_is_created() -> None:
    assert newcli.check_name("") == "a project name is required"
    check = name_check(["blog"])
    assert check("") == "an app name is required"
    assert check("blog") == "app 'blog' is already registered"
    assert check("my-app") is not None and check("posts") is None
    assert requires_check(["blog"])("blog, shop, x") == "unknown app: shop, x"
    assert requires_check(["blog"])(" , ") is None


def test_the_app_report_lists_its_files_and_the_next_step() -> None:
    created = Path("shop/apps/posts")
    lines = report_app(FANCY, "posts", created, Path("hinoki.toml"), ("blog",))
    shown = plain("\n".join(lines))
    assert "Created app posts" in shown and "requires blog" in shown
    assert "migrations/" in shown and "registered app posts" in shown
    assert 'Mount("/posts", posts_routes)' in shown
    assert "uv run hinoki migrations plan" in shown
    quiet = report_app(
        FANCY,
        "posts",
        created,
        Path("hinoki.toml"),
        (),
        no_models=True,
        next_steps=False,
    )
    quiet_shown = plain("\n".join(quiet))
    assert "API-only" in quiet_shown
    assert "migrations/" not in quiet_shown
    assert "migrations plan" not in quiet_shown


def test_the_app_questions_are_asked_with_keys_on_a_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    @contextlib.contextmanager
    def typed(_out: Any) -> Iterator[Iterator[str]]:
        yield tokens("posts\r\r")

    monkeypatch.setattr(appprompts, "keys_available", lambda: True)
    monkeypatch.setattr(appprompts, "raw_keys", typed)
    monkeypatch.setattr(appprompts, "show_banner", lambda *_args: None)
    manifest = argparse.Namespace(apps=[argparse.Namespace(name="blog")])
    args = argparse.Namespace(name=None, requires=None)
    assert appprompts.ask_app(args, manifest, answers(), FANCY) == 0  # type: ignore[arg-type]
    assert (args.name, args.requires) == ("posts", "")
    assert "Requires" in plain(capsys.readouterr().out)


def test_the_project_summary_shows_key_files_and_next_steps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = create("shop", tmp_path)
    newcli.report_created(FANCY, target, "shop")
    shown = plain(capsys.readouterr().out)
    assert "Created shop" in shown and "hinoki.toml" in shown and "tests/" in shown
    steps = plain("\n".join(newcli.next_steps(FANCY, target, False, 1.5)))
    assert "uv sync" in steps and "add an app" in steps and "1.5s" in steps
    done = plain("\n".join(newcli.next_steps(FANCY, target, True, 0.2, app="posts")))
    assert "uv sync" not in done and "add an app" not in done
    assert "200ms" in done


def test_cancelling_reads_differently_with_and_without_colour() -> None:
    assert "Cancelled" in plain(newcli.cancelled(FANCY))
    assert "cancelled; nothing was created" in newcli.cancelled(Style())


def test_a_successful_setup_command_reports_how_long_it_took(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    command = [sys.executable, "-c", "pass"]
    assert newcli.run_tool(command, tmp_path, FANCY)
    assert "-c pass" in plain(capsys.readouterr().out)


def test_the_banner_shimmers_on_an_animated_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("hinoki.terminal.time.sleep", lambda _seconds: None)
    animated = Style(color=True, unicode=True, depth="truecolor", animated=True)
    newcli.show_banner(animated)
    shown = plain(capsys.readouterr().out)
    assert "new project" in shown and "esc to cancel" in shown


def test_key_prompts_run_on_a_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    @contextlib.contextmanager
    def typed(_out: Any) -> Iterator[Iterator[str]]:
        yield tokens("shop\r\r\r\r\r\r\r\rn\r")

    monkeypatch.setattr(newcli, "keys_available", lambda: True)
    monkeypatch.setattr(newcli, "raw_keys", typed)
    args = new_args()
    newcli.ask_everything(args, answers(), FANCY)
    assert args.name == "shop"
    assert "Name" in plain(capsys.readouterr().out)


def test_a_failed_first_app_leaves_the_project_and_says_how_to_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(_name: str, _start: Path | None = None, **_options: Any) -> Path:
        raise ProjectError("no manifest")

    monkeypatch.setattr("hinoki.cli.project.create_app", refuse)
    assert not newcli.create_first_app(Style(), tmp_path, "posts")
    err = capsys.readouterr().err
    assert "no manifest" in err and "hinoki app create posts" in err


def test_a_project_is_not_built_when_its_first_app_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(newcli, "create_first_app", lambda *_args: False)
    args = new_args(name="shop", dir=str(tmp_path), app="posts")
    assert newcli.build_project(args, Style()) is None


def test_keys_are_decoded_across_a_split_character() -> None:
    reader, writer = os.pipe()
    try:
        os.write(writer, "é".encode()[:1])
        os.write(writer, "é".encode()[1:] + b"\x1b[A")
        os.close(writer)
        assert list(read_keys(reader)) == ["é", "up"]
    finally:
        os.close(reader)


def test_raw_keys_reads_single_keys_and_restores_the_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master, slave = pty.openpty()
    out = io.StringIO()
    try:
        with os.fdopen(slave, "r", closefd=False) as stdin:
            monkeypatch.setattr(sys, "stdin", stdin)
            assert keys_available()
            with raw_keys(out) as keys:
                os.write(master, b"ab\x1b[A")
                assert list(itertools.islice(keys, 3)) == ["a", "b", "up"]
                assert not termios.tcgetattr(slave)[3] & termios.ICANON
            assert termios.tcgetattr(slave)[3] & termios.ICANON
        assert out.getvalue() == "\x1b[?25l\x1b[?25h"
    finally:
        os.close(master)
        os.close(slave)


class OneLine:
    summary = "one"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def render(self, style: Style) -> list[str]:
        return ["line"]

    def handle(self, key: str) -> bool:
        self.seen.append(key)
        return key == "enter"

    def value(self) -> str:
        return "".join(self.seen)


def test_a_one_line_widget_is_redrawn_from_the_start_of_its_line() -> None:
    prompter, out = keyed("a\r")
    assert prompter.run(OneLine()) == "aenter"
    assert out.getvalue().count("\r") == 1


def test_ctrl_d_ends_the_input() -> None:
    prompter, _out = keyed("a\x04")
    with pytest.raises(EOFError):
        prompter.run(OneLine())
    prompter, _out = keyed("a")
    with pytest.raises(EOFError):
        prompter.run(OneLine())


def test_widgets_answer_to_clear_digits_and_arrows() -> None:
    text = Text("Name", "Name", "dflt", lambda _text: None)
    for key in ("a", "b", "clear"):
        text.handle(key)
    assert text.value() == "dflt"
    choice = Choice("Pick", "Pick", [("a", ""), ("b", ""), ("c", "")], "a")
    choice.handle("3")
    assert choice.value() == "c"
    choice.handle("9")
    assert choice.value() == "c"
    confirm = Confirm("Sure", "Sure", True)
    confirm.handle("left")
    assert confirm.value() == "no"
    confirm.handle("Y")
    assert confirm.value() == "yes"


def test_a_long_line_is_cut_to_the_width_keeping_its_colours() -> None:
    cut = fit("\x1b[1mhello world\x1b[0m", 6)
    assert plain(cut) == "hello\u2026"
    assert cut.startswith("\x1b[1m") and cut.endswith(RESET)
    assert fit("short", 20) == "short"


def test_durations_show_milliseconds_below_a_second_and_seconds_above() -> None:
    assert elapsed(0.2) == "200ms" and elapsed(2.34) == "2.3s"


def test_the_tagline_can_carry_a_glint_and_a_detail() -> None:
    assert plain(FANCY.tagline("v1")) == f"{TAGLINE}  v1"
    glinted = FANCY.tagline("", glint=3)
    assert plain(glinted) == TAGLINE and glinted != FANCY.tagline("")


def test_questions_and_headings_are_marked_on_a_coloured_terminal() -> None:
    assert plain(FANCY.question("Name", "x")).strip().startswith("? Name [x]")
    assert plain(FANCY.heading("Name")).strip() == "? Name"
    assert Style().question("Name", "") == "Name: "
    assert Style().intro("t") == ""


class StepEvent:
    """An ``Event`` whose wait times out twice, then reports the set flag."""

    def __init__(self) -> None:
        self.waits = 0

    def wait(self, _timeout: float) -> bool:
        self.waits += 1
        return self.waits > 2

    def set(self) -> None:
        return None


def test_the_spinner_draws_frames_and_clears_its_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        terminal,
        "threading",
        types.SimpleNamespace(Event=StepEvent, Thread=threading.Thread),
    )
    out = io.StringIO()
    animated = Style(color=True, unicode=True, animated=True)
    with animated.spinner("work", out):
        pass
    drawn = out.getvalue()
    assert drawn.count("work") == 2 and drawn.startswith("\x1b[?25l")
    assert drawn.endswith(f"\r{CLEAR_LINE}\x1b[?25h")


def test_the_app_questions_fall_back_to_lines_and_can_be_cancelled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(appprompts, "show_banner", lambda *_args: None)
    manifest = argparse.Namespace(apps=[])
    args = argparse.Namespace(name=None, requires=None)
    asked = answers("posts")
    assert appprompts.ask_app(args, manifest, asked, Style()) == 0  # type: ignore[arg-type]
    assert args.name == "posts"

    def interrupted(_prompt: str) -> str:
        raise KeyboardInterrupt

    blank = argparse.Namespace(name=None, requires=None)
    assert appprompts.ask_app(blank, manifest, interrupted, Style()) == 130  # type: ignore[arg-type]
    assert "nothing was created" in capsys.readouterr().err


def test_a_path_under_the_home_directory_is_shown_with_a_tilde(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert shown_path(tmp_path / "shop") == f"~{os.sep}shop"
