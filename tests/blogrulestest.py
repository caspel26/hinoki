"""The blog keeps the promises its rules state (and the rules can fail)."""

import pytest

from examples.blog.main import create_app
from examples.blog.rules import blog_rules
from hinoki import Actions, HinokiAPI, Request, delete
from hinoki.__main__ import main
from hinoki.rules import Project, RuleSet, failing

SECRET = "b" * 40
CHECK = [
    "check",
    "--app",
    "examples.blog.main:create_app",
    "--rules",
    "examples.blog.rules:blog_rules",
    "--package",
    "examples",
]


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLOG_DATABASE_URL", "postgres://unused")
    monkeypatch.setenv("BLOG_ADMIN_SECRET", SECRET)


def test_the_blog_passes_its_own_rules_from_the_command_line(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
) -> None:
    monkeypatch.chdir(str(tmp_path))
    assert main(CHECK) == 0
    assert "4 rule(s): 0 violated" in capsys.readouterr().out


def test_the_blog_app_enforces_them_at_startup() -> None:
    app = create_app("postgres://unused", SECRET)
    assert app.rules is blog_rules and app.enforce_rules
    assert app.asgi() is not None  # would raise RegistrationError on a violation


def test_a_new_unprotected_write_would_be_caught() -> None:
    app = create_app("postgres://unused", SECRET)

    @delete("/posts/purge")
    def purge(request: Request) -> None: ...

    purge.__module__ = "examples.blog.api"
    app.include(purge)
    found = RuleSet(*blog_rules.rules[:2]).run(Project(app))
    messages = " ".join(d.message for d in failing(found))
    assert "DELETE /posts/purge" in messages and "transaction=True" in messages


def test_the_rule_names_are_stable_ids() -> None:
    ids = [rule.id for rule in blog_rules.rules]
    assert ids == ["HNK-ACC-001", "HNK-TXN-001", "HNK-SCH-001", "HNK-IMP-001"]
    assert Actions.require_access().id == ids[0] and HinokiAPI is not None
