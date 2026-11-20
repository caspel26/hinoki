"""The HTTP decorators live in ``hinoki.http``; the old top-level names are gone."""

import ast
import importlib
import sys
from pathlib import Path

import pytest

import hinoki
from hinoki import http

VERBS = {"delete", "get", "patch", "post", "put", "query"}
HTTP_TYPES = {"Request", "Response", "Middleware", "MiddlewareRequest", "Next"}
ROOT = Path(__file__).resolve().parents[1]
SOURCES = ("python", "tests", "examples", "benchmarks")


def test_the_namespace_exports_exactly_the_verbs_and_types() -> None:
    assert set(http.__all__) == VERBS | HTTP_TYPES
    assert all(callable(getattr(http, name)) for name in VERBS)
    assert http.Request is hinoki.Request
    assert http.Response is hinoki.Response


@pytest.mark.parametrize("name", sorted(VERBS))
def test_the_old_top_level_names_do_not_exist(name: str) -> None:
    assert not hasattr(hinoki, name)
    assert name not in hinoki.__all__
    with pytest.raises(ImportError):
        exec(f"from hinoki import {name}")  # noqa: S102 - the point of the test


def test_the_former_reply_name_is_a_deprecated_alias_of_response() -> None:
    with pytest.warns(DeprecationWarning, match=r"hinoki\.http\.response\.Response"):
        from hinoki import Reply  # noqa: PLC0415 - the import is the point

    assert Reply is hinoki.Response
    assert "Reply" not in hinoki.__all__
    with pytest.raises(AttributeError, match="no attribute 'Missing'"):
        _ = hinoki.Missing  # type: ignore[attr-defined]


def test_the_former_reply_module_is_a_deprecated_shim() -> None:
    sys.modules.pop("hinoki.reply", None)
    with pytest.warns(DeprecationWarning, match=r"hinoki\.http\.response"):
        reply = importlib.import_module("hinoki.reply")

    assert reply.Reply is reply.Response is hinoki.Response
    assert reply.set_cookie is hinoki.set_cookie


def test_no_first_party_file_imports_the_old_names() -> None:
    offenders = [
        offender
        for folder in SOURCES
        for path in (ROOT / folder).rglob("*.py")
        if not is_exempt(path)
        for offender in old_name_imports(path)
    ]
    assert offenders == []


def is_exempt(path: Path) -> bool:
    return "hinoki/http.py" in str(path)


def old_name_imports(path: Path) -> list[str]:
    offenders = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module == "hinoki":
            names = {alias.name for alias in node.names} & VERBS
            if names:
                offenders.append(f"{path.relative_to(ROOT)}: {sorted(names)}")
    return offenders
