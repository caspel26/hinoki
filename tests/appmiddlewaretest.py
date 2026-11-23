"""``[project] middleware`` and ``[project] rules`` in ``hinoki.toml``."""

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from hinoki import Hinoki
from hinoki.__main__ import main
from hinoki.project import (
    ProjectError,
    load_manifest,
    manifest_middleware,
    manifest_rules,
)

PACKAGE = "listsproj"

MIDDLEWARE = """from hinoki.http import Middleware


class First(Middleware): ...


class Second(Middleware): ...


class Third(Middleware): ...


pair = [First(), Second()]
single = Third()
not_middleware = object()
"""

RULES = """from hinoki.rules import Diagnostic, Imports, Rule, RuleSet


class Always(Rule):
    id = "TST-001"
    explanation = "always violated"

    def check(self, project):
        return [self.diagnostic("broken", "x.py:1", subject="x")]


class Other(Rule):
    id = "TST-002"
    explanation = "never violated"

    def check(self, project):
        return []


first = RuleSet(Always())
second = RuleSet(Other())
clashing = RuleSet(Always())
imports_pair = RuleSet(Imports.forbid("a", "b"), Imports.forbid("c", "d"))
imports_other = RuleSet(Imports.forbid("e", "f"))
not_rules = [Always()]
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    package = tmp_path / PACKAGE
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "mw.py").write_text(MIDDLEWARE, encoding="utf-8")
    (package / "rules.py").write_text(RULES, encoding="utf-8")
    (package / "app.py").write_text(
        "from hinoki import Hinoki\n\napp = Hinoki()\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    yield tmp_path
    for module in [m for m in sys.modules if m.split(".")[0] == PACKAGE]:
        del sys.modules[module]


def write_manifest(project: Path, lists: str) -> None:
    (project / "hinoki.toml").write_text(
        f'[project]\napplication = "{PACKAGE}.app:app"\n{lists}', encoding="utf-8"
    )


def test_middleware_keeps_the_listed_order_and_flattens_sequences(
    project: Path,
) -> None:
    write_manifest(
        project, f'middleware = ["{PACKAGE}.mw:single", "{PACKAGE}.mw:pair"]\n'
    )

    found = manifest_middleware(project)

    assert [type(item).__name__ for item in found] == ["Third", "First", "Second"]
    assert Hinoki(middleware=found).middleware == tuple(found)


def test_rules_merge_in_listed_order(project: Path) -> None:
    write_manifest(
        project, f'rules = ["{PACKAGE}.rules:second", "{PACKAGE}.rules:first"]\n'
    )

    merged = manifest_rules(project)

    assert [rule.id for rule in merged.rules] == ["TST-002", "TST-001"]


def test_no_lists_means_nothing_to_load(project: Path) -> None:
    write_manifest(project, "")

    assert manifest_middleware(project) == []
    assert manifest_rules(project).rules == ()


def test_rules_sharing_an_id_but_not_arguments_are_allowed(project: Path) -> None:
    write_manifest(
        project,
        f'rules = ["{PACKAGE}.rules:imports_pair", "{PACKAGE}.rules:imports_other"]\n',
    )

    merged = manifest_rules(project)

    assert [rule.id for rule in merged.rules] == ["HNK-IMP-001"] * 3


def test_the_same_rule_listed_twice_names_both_entries(project: Path) -> None:
    write_manifest(
        project,
        f'rules = ["{PACKAGE}.rules:first", "{PACKAGE}.rules:clashing"]\n',
    )

    with pytest.raises(ProjectError) as error:
        manifest_rules(project)

    message = str(error.value)
    assert "TST-001" in message
    assert f"{PACKAGE}.rules:first" in message
    assert f"{PACKAGE}.rules:clashing" in message


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("middleware", '"pkg.mod:attr"'),
        ("middleware", "[1]"),
        ("middleware", '["no_attribute"]'),
        ("rules", '"pkg.mod:attr"'),
        ("rules", "[1]"),
        ("rules", '["pkg.mod:"]'),
    ],
)
def test_a_list_of_anything_else_is_rejected_at_load(
    project: Path, key: str, value: str
) -> None:
    write_manifest(project, f"{key} = {value}\n")

    with pytest.raises(ProjectError, match=f"project.{key} must be a list"):
        load_manifest()


@pytest.mark.parametrize(
    ("key", "target", "message"),
    [
        ("middleware", "nope.mod:x", "cannot load the middleware nope.mod:x"),
        ("middleware", f"{PACKAGE}.mw:missing", "cannot load the middleware"),
        ("middleware", f"{PACKAGE}.mw:not_middleware", "async def __call__"),
        ("rules", "nope.mod:x", "cannot load the rules nope.mod:x"),
        ("rules", f"{PACKAGE}.rules:not_rules", "must be a hinoki.rules.RuleSet"),
    ],
)
def test_an_unusable_target_raises_a_project_error(
    project: Path, key: str, target: str, message: str
) -> None:
    write_manifest(project, f'{key} = ["{target}"]\n')

    loader = manifest_middleware if key == "middleware" else manifest_rules
    with pytest.raises(ProjectError, match=message):
        loader(project)


def test_check_runs_the_manifest_rules_unless_rules_are_given(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_manifest(
        project, f'rules = ["{PACKAGE}.rules:first", "{PACKAGE}.rules:second"]\n'
    )

    assert main(["check"]) == 1
    out = capsys.readouterr().out
    assert "TST-001" in out
    assert "2 rule(s)" in out

    assert main(["check", "--rules", f"{PACKAGE}.rules:second"]) == 0
