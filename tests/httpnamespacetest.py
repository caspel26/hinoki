"""The HTTP decorators live in ``hinoki.http``; the old top-level names are gone."""

import ast
from pathlib import Path

import pytest

import hinoki
from hinoki import http

VERBS = {"delete", "get", "patch", "post", "put"}
ROOT = Path(__file__).resolve().parents[1]
SOURCES = ("python", "tests", "examples", "benchmarks")


def test_the_namespace_exports_exactly_the_five_verbs() -> None:
    assert set(http.__all__) == VERBS
    assert all(callable(getattr(http, name)) for name in VERBS)


@pytest.mark.parametrize("name", sorted(VERBS))
def test_the_old_top_level_names_do_not_exist(name: str) -> None:
    assert not hasattr(hinoki, name)
    assert name not in hinoki.__all__
    with pytest.raises(ImportError):
        exec(f"from hinoki import {name}")  # noqa: S102 - the point of the test


def test_no_first_party_file_imports_the_old_names() -> None:
    offenders = []
    for folder in SOURCES:
        for path in (ROOT / folder).rglob("*.py"):
            if "hinoki/http.py" in str(path) or ("/typing/" in str(path) and False):
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom) and node.module == "hinoki":
                    names = {a.name for a in node.names} & VERBS
                    if names:
                        offenders.append(f"{path.relative_to(ROOT)}: {sorted(names)}")
    assert offenders == []
