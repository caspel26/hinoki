"""``Hinoki()`` loads the events and middleware hinoki.toml declares."""

import importlib
import sys
from pathlib import Path

import pytest

from hinoki.http.middleware import Middleware

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
}
MANIFEST = """[project]
application = "{package}.app:{attribute}"

[[apps]]
name = "posts"
models = "{package}.models"
migrations = "{package}.migrations"
events = "{package}.events:handlers"
middleware = "{package}.mw:middleware"
requires = []
"""
APPS = {
    "app": "from hinoki import Hinoki\n\napp = Hinoki()\n",
    "explicit": "from hinoki import Hinoki\n\napp = Hinoki(events=(), middleware=())\n",
}


@pytest.mark.parametrize(("name", "loaded"), [("app", 1), ("explicit", 0)])
def test_hinoki_reads_events_and_middleware_unless_given(
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
