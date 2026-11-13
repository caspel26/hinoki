"""Regressions for the independent review of the rules engine and MCP."""

import datetime
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from hinoki import Actions, Hinoki, Imports, Request, Schema, Schemas, get, mcp
from hinoki.__main__ import main
from hinoki.imports import imports_of
from hinoki.mcp import DevProject, Server, serve
from hinoki.rules import (
    Diagnostic,
    Phase,
    Project,
    Rule,
    RuleSet,
    Status,
    failing,
    qualified,
)
from hinoki.waivers import WaiverError, apply_waivers, load_waivers
from tests.projects.inherited import create_app
from tests.projects.rulesapp import create_app as plain_app

TODAY = datetime.date(2026, 11, 13)


def project(app: Hinoki) -> Project:
    return Project(app)


def test_inherited_actions_are_identified_by_the_class_they_are_mounted_on() -> None:
    app = create_app()
    names = sorted(qualified(r) for r in app.routes)
    assert "tests.projects.inherited.Posts.remove" in names
    assert "tests.projects.inherited.Comments.remove" in names
    found = RuleSet(Actions.require_access(match="tests.projects.inherited.*")).run(
        project(app)
    )
    assert sorted(d.subject for d in found) == [
        "tests.projects.inherited.Comments.remove",
        "tests.projects.inherited.Comments.untyped",
        "tests.projects.inherited.Posts.remove",
        "tests.projects.inherited.Posts.untyped",
    ]


def test_exceptions_match_the_full_name_not_a_bare_function_name() -> None:
    app = create_app()
    rules = RuleSet(Actions.require_access(except_=["remove"]))
    assert len(rules.run(project(app))) == 4  # "remove" exempts nothing
    rules = RuleSet(Actions.require_access(except_=["*.Posts.remove"]))
    assert "tests.projects.inherited.Posts.remove" not in {
        d.subject for d in rules.run(project(app))
    }


class NeedsDatabase(Rule):
    id = "HNK-TST-010"
    explanation = "needs a database"
    effects = ("database",)

    def check(self, project: Project) -> list[Diagnostic]:
        return []


def test_a_rule_that_could_not_run_fails_the_build() -> None:
    found = RuleSet(NeedsDatabase()).run(project(plain_app()))
    assert [d.status for d in found] == [Status.UNVERIFIABLE]
    assert failing(found) == found
    assert RuleSet(NeedsDatabase()).run(project(plain_app()), allow_effects=True) == []


@pytest.mark.parametrize(
    "target", ["*.*", "s*", "*a*", "[!x]*", "*->*", "ab.*", "x", "*"]
)
def test_overbroad_waiver_targets_are_refused(tmp_path: Path, target: str) -> None:
    path = tmp_path / "w.toml"
    path.write_text(
        f'[[waiver]]\nrule="HNK-ACC-001"\ntarget="{target}"\nreason="r"\nowner="o"\n'
    )
    if target in {"x"}:
        assert load_waivers(path)  # an exact name is bounded
        return
    with pytest.raises(WaiverError, match="not bounded"):
        load_waivers(path)


def test_waiver_fields_must_be_text(tmp_path: Path) -> None:
    path = tmp_path / "w.toml"
    path.write_text('[[waiver]]\nrule="A"\ntarget="x"\nreason=true\nowner=[]\n')
    with pytest.raises(WaiverError, match="must be text"):
        load_waivers(path)


def test_a_could_not_run_diagnostic_cannot_be_waived(tmp_path: Path) -> None:
    path = tmp_path / "w.toml"
    path.write_text(
        '[[waiver]]\nrule="HNK-IMP-001"\ntarget="HNK-IMP-001"\nreason="r"\nowner="o"\n'
    )
    found = RuleSet(Imports.forbid(source="a.b", target="c.d")).run(
        project(plain_app()), Phase.STATIC
    )
    result = apply_waivers(found, load_waivers(path), TODAY)
    assert result[0].status is Status.UNVERIFIABLE
    assert failing(result)


def test_a_rule_that_matches_nothing_says_so() -> None:
    found = RuleSet(Actions.require_access(match="typo.*")).run(project(plain_app()))
    assert [d.status for d in found] == [Status.UNVERIFIABLE]
    assert failing(found)


def test_a_rule_that_exits_is_a_failure_not_a_pass() -> None:
    class Exits(Rule):
        id = "HNK-TST-011"
        explanation = "exits"

        def check(self, project: Project) -> list[Diagnostic]:
            raise SystemExit(0)

    found = RuleSet(Exits()).run(project(plain_app()))
    assert failing(found)


class Public(Schema):
    name: str


class Private(Public):
    password_hash: str


@get("/who")
def who(request: Request) -> Public:
    return Private(name="a", password_hash="secret")  # noqa: S106


@get("/mystery")
def mystery(request: Request):  # type: ignore[no-untyped-def]
    return {}


def test_exposure_sees_subclasses_and_open_return_types() -> None:
    app = Hinoki()
    app.include(who, mystery)
    found = RuleSet(Schemas.forbid_exposure("password_hash")).run(project(app))
    assert {(d.status.value, d.subject.rsplit(".", 1)[-1]) for d in found} == {
        ("violated", "password_hash"),
        ("unverifiable", "mystery"),
    }


def write(root: Path, text: str) -> Path:
    path = root / "m.py"
    path.write_text(text)
    return path


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "from importlib import import_module as im\nim('shop.api.views')",
            {"shop.api.views"},
        ),
        (
            "import importlib\nimportlib.import_module('.api.views', 'shop')",
            {"shop.api.views"},
        ),
        (
            "import importlib\nimportlib.import_module('..api', __package__)",
            {"shop.api"},
        ),
        ("__import__('shop', fromlist=['api'])", {"shop", "shop.api"}),
        ("__import__(name='shop.api')", {"shop.api"}),
        ("import importlib\nimportlib.import_module(name)", {""}),
        ("import importlib\nimportlib.import_module('.x')", {""}),
    ],
)
def test_dynamic_import_forms_are_resolved_or_reported(
    tmp_path: Path, source: str, expected: set[str]
) -> None:
    found = {i.target for i in imports_of("shop.domain.m", write(tmp_path, source))}
    assert expected <= found


def test_an_import_rule_with_a_mistyped_source_is_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    found = RuleSet(Imports.forbid(source="shop.domian", target="shop.api")).run(
        Project(Hinoki(), ("shop",)), Phase.STATIC
    )
    assert failing(found)


class Chatty(Rule):
    id = "HNK-TST-012"
    explanation = {1, 2}  # type: ignore[assignment]  # noqa: RUF012 - not JSON

    def check(self, project: Project) -> list[Diagnostic]:
        print("debug from a rule")
        return []


def server_for(*rules: Rule, tmp_path: Path) -> Server:
    return Server(
        DevProject(
            plain_app(), RuleSet(*rules), waivers=tmp_path / "x.toml", today=TODAY
        )
    )


def test_tools_survive_values_that_are_not_json(tmp_path: Path) -> None:
    server = server_for(Chatty(), tmp_path=tmp_path)
    out = io.StringIO()
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "list_rules", "arguments": {}},
    }
    serve(server, io.StringIO(json.dumps(request) + "\n"), out)
    answer = json.loads(out.getvalue())
    assert answer["result"]["isError"] is False
    assert answer["result"]["structuredContent"]["rules"][0]["explanation"]


def test_the_loop_survives_a_crash_and_deep_nesting(tmp_path: Path) -> None:
    server = server_for(tmp_path=tmp_path)
    lines = "\n".join(
        [
            "[" * 200000,
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        ]
    )
    out = io.StringIO()
    serve(server, io.StringIO(lines + "\n"), out)
    answers = [json.loads(line) for line in out.getvalue().splitlines()]
    assert answers[0]["error"]["code"] == -32700
    assert answers[1]["result"] == {}


@pytest.mark.parametrize(
    ("message", "reply"),
    [
        ({"jsonrpc": "2.0", "id": {"a": 1}, "method": "ping"}, "error"),
        ({"jsonrpc": "2.0", "id": True, "method": "ping"}, "error"),
        ({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []}, "error"),
        ({"jsonrpc": "2.0", "method": "ping", "params": 3}, None),
    ],
)
def test_ids_and_params_are_checked_strictly(
    tmp_path: Path, message: dict[str, Any], reply: str | None
) -> None:
    answer = server_for(tmp_path=tmp_path).handle(message)
    assert (None if answer is None else "error" in answer and "error") == reply


def test_explain_finds_inherited_actions_by_class_and_exact_subjects(
    tmp_path: Path,
) -> None:
    project_ = DevProject(
        create_app(),
        RuleSet(Actions.require_access()),
        waivers=tmp_path / "x.toml",
        today=TODAY,
    )
    server = Server(project_)

    def explain(name: str) -> dict[str, Any]:
        answer = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "explain_action", "arguments": {"action": name}},
            }
        )
        assert answer is not None
        return answer["result"]["structuredContent"]  # type: ignore[no-any-return]

    data = explain("Posts.remove")
    assert data["id"] == "tests.projects.inherited.Posts.remove"
    assert [d["subject"] for d in data["diagnostics"]] == [data["id"]]
    assert "ambiguous" in explain("remove")["error"]


def test_print_from_rules_does_not_reach_the_protocol_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Args:
        target = "tests.projects.rulesapp:create_app"
        rules = "tests.projects.rulesapp:chatty"
        package = None
        models = None
        waivers = None
        scenarios = "off"

    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "check_project", "arguments": {}},
                }
            )
            + "\n"
        ),
    )
    monkeypatch.chdir(tmp_path)
    assert mcp.run_mcp(Args()) == 0  # type: ignore[arg-type]
    captured = capsys.readouterr()
    assert [json.loads(line)["id"] for line in captured.out.splitlines()] == [1]
    assert "debug from a rule" in captured.err


def test_check_fails_for_a_missing_explicit_waiver_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "check",
            "tests.projects.rulesapp:create_app",
            "--waivers",
            str(tmp_path / "typo.toml"),
        ]
    )
    assert code == 2
    assert "does not exist" in capsys.readouterr().err


def test_check_allow_effects_runs_effectful_rules(
    capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = [
        "check",
        "tests.projects.rulesapp:create_app",
        "--rules",
        "tests.projects.rulesapp:effectful",
    ]
    assert main(arguments) == 1
    assert main([*arguments, "--allow-effects"]) == 0
