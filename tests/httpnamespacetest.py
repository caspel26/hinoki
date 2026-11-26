"""The HTTP decorators live in ``hinoki.http``; the old top-level names are gone."""

import ast
import importlib
import sys
from pathlib import Path

import pytest

import hinoki
from hinoki import http

VERBS = {"delete", "get", "patch", "post", "put", "query"}
HTTP_TYPES = {
    "Request",
    "Response",
    "Cors",
    "Middleware",
    "MiddlewareRequest",
    "Next",
    "Stream",
    "EventStream",
    "Event",
    "File",
    "set_cookie",
}
WEBSOCKET = {"websocket", "WebSocket", "WebSocketClosed", "CloseCode"}
ROOT = Path(__file__).resolve().parents[1]
SOURCES = ("python", "tests", "examples", "benchmarks")


def test_the_namespace_exports_exactly_the_verbs_and_types() -> None:
    assert set(http.__all__) == VERBS | HTTP_TYPES | WEBSOCKET
    assert all(callable(getattr(http, name)) for name in VERBS)
    assert http.Request is hinoki.Request
    assert http.Response is hinoki.Response


@pytest.mark.parametrize("name", sorted(VERBS))
def test_the_old_top_level_names_do_not_exist(name: str) -> None:
    assert not hasattr(hinoki, name)
    assert name not in hinoki.__all__
    with pytest.raises(ImportError):
        exec(f"from hinoki import {name}")  # noqa: S102 - the point of the test


def test_the_former_reply_name_is_removed() -> None:
    assert not hasattr(hinoki, "Reply")
    assert "Reply" not in hinoki.__all__
    with pytest.raises(ImportError):
        exec("from hinoki import Reply")  # noqa: S102 - the point of the test
    with pytest.raises(AttributeError, match="has no attribute 'Missing'"):
        _ = hinoki.Missing  # type: ignore[attr-defined]


def test_the_former_reply_module_is_removed() -> None:
    sys.modules.pop("hinoki.reply", None)
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("hinoki.reply")


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
            names = {alias.name for alias in node.names} & (VERBS | {"Reply"})
            if names:
                offenders.append(f"{path.relative_to(ROOT)}: {sorted(names)}")
        elif isinstance(node, ast.ImportFrom) and node.module == "hinoki.reply":
            offenders.append(f"{path.relative_to(ROOT)}: hinoki.reply")
    return offenders


def test_public_modules_export_what_applications_import_and_hide_internals() -> None:
    import hinoki.db  # noqa: PLC0415 - the module objects are the subject
    import hinoki.http.middleware  # noqa: PLC0415
    import hinoki.orm  # noqa: PLC0415

    assert {"DatabaseModule", "AtomicScope"} <= set(hinoki.db.__all__)
    assert "TextParser" in hinoki.__all__
    assert "check_middleware" not in hinoki.http.middleware.__all__
    internal = {"IN_CHUNK", "MARK", "OPERATORS", "SEPARATOR", "SNAPSHOT"}
    internal |= {"FieldMeta", "condition", "database_var", "fetch_in"}
    assert not internal & set(hinoki.orm.__all__)
    assert all(
        hasattr(module, name)
        for module in (hinoki.db, hinoki.orm, hinoki)
        for name in module.__all__
    )
