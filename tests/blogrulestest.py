"""The blog keeps the promises its rules state (and the rules can fail)."""

import pytest
from blog.main import app
from blog.routes import routes
from blog.rules import blog_rules
from blog.settings import BlogSettings

from hinoki import Actions, Hinoki, Request, http
from hinoki.__main__ import main
from hinoki.rules import Project, RuleSet, failing

CHECK = [
    "check",
    "blog.main:app",
    "--rules",
    "blog.rules:blog_rules",
    "--package",
    "blog",
]


def test_the_blog_passes_its_own_rules_from_the_command_line(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
) -> None:
    monkeypatch.chdir(str(tmp_path))
    assert main(CHECK) == 0
    assert "11 rule(s): 0 violated, 0 unverifiable" in capsys.readouterr().out


def test_the_blog_app_enforces_them_at_startup() -> None:
    assert app.rules is blog_rules and app.enforce_rules
    assert app.asgi() is not None  # would raise RegistrationError on a violation


def test_local_settings_default_to_sqlite() -> None:
    settings = BlogSettings.load(env={})
    assert settings.database.url.reveal() == "sqlite://./.hinoki/local.sqlite3"


def test_a_new_unprotected_write_would_be_caught() -> None:
    # A second app, so the shared module app is never changed by this test.
    fresh = Hinoki(settings=BlogSettings.load(), include=routes)

    @http.delete("/posts/purge")
    def purge(request: Request) -> None: ...

    purge.__module__ = "blog.api"
    fresh.include(purge)
    chosen = [r for r in blog_rules.rules if r.id in {"HNK-ACC-001", "HNK-TXN-001"}]
    found = RuleSet(*chosen).run(Project(fresh))
    messages = " ".join(d.message for d in failing(found))
    assert "DELETE /posts/purge" in messages and "atomic=True" in messages


def test_the_rule_names_are_stable_ids() -> None:
    ids = [rule.id for rule in blog_rules.rules]
    assert ids == [
        "HNK-ACC-001",
        "HNK-ACC-002",
        "HNK-API-001",
        "HNK-API-002",
        "HNK-API-003",
        "HNK-TXN-001",
        "HNK-SCH-001",
        "HNK-SCH-002",
        "HNK-DB-001",
        "HNK-DB-002",
        "HNK-IMP-001",
    ]
    assert Actions.require_access().id == ids[0] and Hinoki is not None
