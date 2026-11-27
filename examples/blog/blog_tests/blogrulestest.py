"""The blog keeps the promises its rules state (and the rules can fail)."""

import os
import subprocess
import sys
from pathlib import Path

from blog.main import app
from blog.routes import routes
from blog.rules import blog_rules
from blog.settings import BlogSettings
from blogworld import ADMIN_SECRET, JWT_SECRET

from hinoki import Hinoki, Request, http
from hinoki.rules import Actions, Project, RuleSet, failing

ROOT = Path(__file__).parents[1]


def check(**env: str) -> str:
    """``hinoki check`` from the example's directory, as the README runs it."""
    run = subprocess.run(
        [sys.executable, "-m", "hinoki", "check"],
        cwd=ROOT,
        env={**os.environ, "BLOG_ADMIN__SECRET": ADMIN_SECRET, **env},
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    return run.stdout


def test_the_blog_passes_its_own_rules_from_the_command_line() -> None:
    assert "11 rule(s): 0 violated, 0 unverifiable" in check()


def test_the_waiver_covers_login_only_when_login_is_switched_on() -> None:
    assert "0 waived" in check()
    waived = check(BLOG_JWT_SECRET=JWT_SECRET)
    assert "HNK-TXN-001 [waived] POST /auth/login" in waived
    assert "11 rule(s): 0 violated, 0 unverifiable, 1 waived" in waived


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

    purge.__module__ = "blog.apps.blog.api"
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
