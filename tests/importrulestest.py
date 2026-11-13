"""The static Imports rule reads source files and executes nothing."""

from pathlib import Path

import pytest

from hinoki import HinokiAPI, Imports
from hinoki.imports import absolute, within
from hinoki.rules import Phase, Project, RuleSet, Status


def write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def blog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    write(tmp_path, "shop/__init__.py", "")
    write(tmp_path, "shop/api/__init__.py", "")
    write(tmp_path, "shop/api/views.py", "VALUE = 1\n")
    write(tmp_path, "shop/domain/__init__.py", "from . import rules\n")
    write(tmp_path, "shop/domain/rules.py", "import os\nfrom shop.api import views\n")
    write(tmp_path, "shop/domain/more.py", "from ..api.views import VALUE\n")
    write(tmp_path, "shop/domain/lazy.py", "def f():\n    import shop.api\n")
    write(
        tmp_path,
        "shop/domain/dynamic.py",
        "import importlib\n"
        "importlib.import_module('shop.api.views')\n"
        "name = 'x'\n__import__(name)\n",
    )
    write(tmp_path, "shop/web/__init__.py", "from shop.domain import rules\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    return "shop"


def name_of(location: str) -> str:
    return Path(location.rsplit(":", 1)[0]).name


def run(package: str, rule_source: str = "shop.domain") -> list[tuple[str, str, str]]:
    rules = RuleSet(Imports.forbid(source=rule_source, target="shop.api"))
    found = rules.run(Project(HinokiAPI(), (package,)), Phase.STATIC)
    return [(d.location, d.status.value, d.message) for d in found]


def test_forbidden_imports_are_reported_with_file_and_line(blog: str) -> None:
    found = run(blog)
    locations = {(name_of(loc), status) for loc, status, _ in found}
    assert ("rules.py", "violated") in locations  # from shop.api import views
    assert ("more.py", "violated") in locations  # relative ..api.views
    assert ("lazy.py", "violated") in locations  # import inside a function
    assert ("dynamic.py", "violated") in locations  # literal import_module
    assert ("dynamic.py", "unverifiable") in locations  # __import__(name)
    assert all(name_of(loc) != "__init__.py" for loc, _, _ in found)
    line = next(loc for loc, _, msg in found if "shop.api" in msg and "rules" in loc)
    assert line.endswith("rules.py:2")


def test_only_the_source_package_is_checked(blog: str) -> None:
    assert run(blog, "shop.web") == []
    assert run(blog, "shop.api") == []


def test_without_packages_the_rule_says_it_cannot_decide() -> None:
    rules = RuleSet(Imports.forbid(source="a", target="b"))
    found = rules.run(Project(HinokiAPI()), Phase.STATIC)
    assert [d.status for d in found] == [Status.UNVERIFIABLE]


def test_an_unknown_package_is_an_error_not_a_pass() -> None:
    rules = RuleSet(Imports.forbid(source="a", target="b"))
    found = rules.run(Project(HinokiAPI(), ("definitely_not_a_package",)))
    assert found[0].status is Status.UNVERIFIABLE
    assert "cannot find package" in found[0].message


def test_name_helpers() -> None:
    assert within("a.b", "a") and within("a", "a")
    assert not within("ab", "a")
    assert absolute("shop.domain.rules", False, 1, "x") == "shop.domain.x"
    assert absolute("shop.domain", True, 1, "x") == "shop.domain.x"
    assert absolute("shop.domain.rules", False, 2, "x") == "shop.x"
    assert absolute("m", False, 0, "os") == "os"
