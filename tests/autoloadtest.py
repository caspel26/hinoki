"""``Hinoki()`` loads the events, middleware and rules hinoki.toml declares."""

import importlib
import sys
from pathlib import Path

import pytest

from hinoki.http.middleware import Middleware
from hinoki.rules import RuleSet

MODULES = {
    "events.py": """from dataclasses import dataclass

from hinoki import events


@dataclass(frozen=True)
class Ping:
    text: str


handlers = events.Handlers("posts")


@handlers.after_commit(Ping)
async def on_ping(event: Ping) -> None: ...
""",
    "mw.py": """from hinoki.http import Middleware


class Stamp(Middleware): ...


middleware = [Stamp()]
""",
    "rules.py": """from hinoki.rules import Actions, RuleSet

project_rules = RuleSet(Actions.require_access())
""",
}
MANIFEST = """[project]
application = "{package}.app:{attribute}"
middleware = ["{package}.mw:middleware"]
rules = ["{package}.rules:project_rules"]

[[apps]]
name = "posts"
models = "{package}.models"
migrations = "{package}.migrations"
events = "{package}.events:handlers"
requires = []
"""
APPS = {
    "app": "from hinoki import Hinoki\n\napp = Hinoki()\n",
    "explicit": (
        "from hinoki import Hinoki\n"
        "from hinoki.rules import RuleSet\n\n"
        "app = Hinoki(events=(), middleware=(), rules=RuleSet())\n"
    ),
}


@pytest.mark.parametrize(("name", "loaded"), [("app", 1), ("explicit", 0)])
def test_hinoki_reads_the_manifest_lists_unless_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, loaded: int
) -> None:
    package = f"autoload_{name}"
    root = tmp_path / package
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    for file, text in MODULES.items():
        (root / file).write_text(text, encoding="utf-8")
    (root / "app.py").write_text(APPS[name], encoding="utf-8")
    (tmp_path / "hinoki.toml").write_text(
        MANIFEST.format(package=package, attribute="app"), encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "modules", dict(sys.modules))

    app = importlib.import_module(f"{package}.app").app

    middleware: tuple[Middleware, ...] = tuple(app.middleware)
    assert len(middleware) == loaded
    assert len(app.events.registrations) == loaded
    assert isinstance(app.rules, RuleSet)
    assert len(app.rules.rules) == loaded
