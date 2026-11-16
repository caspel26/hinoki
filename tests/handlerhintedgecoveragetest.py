"""Python-version-specific handler annotation resolution branches."""

from __future__ import annotations

import importlib
import inspect
import sys
import typing
from types import SimpleNamespace
from typing import Any, cast

import pytest

from hinoki import handlerhints


def test_python_314_signature_requests_unresolved_annotation_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def signature(handler: Any, *, annotation_format: str) -> inspect.Signature:
        captured["handler"] = handler
        captured["format"] = annotation_format
        return inspect.Signature()

    monkeypatch.setitem(
        handlerhints.__dict__, "sys", SimpleNamespace(version_info=(3, 14))
    )
    monkeypatch.setitem(
        handlerhints.__dict__, "inspect", SimpleNamespace(signature=signature)
    )
    monkeypatch.setitem(
        handlerhints.__dict__,
        "annotationlib",
        SimpleNamespace(Format=SimpleNamespace(STRING="source")),
    )

    def endpoint() -> None:
        return None

    assert handlerhints.signature_of(endpoint) == inspect.Signature()
    assert captured == {"handler": endpoint, "format": "source"}


def test_python_314_hints_fallback_resolves_source_in_module_globals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def endpoint(value: int) -> str:
        return str(value)

    real_get_type_hints = typing.get_type_hints

    def get_type_hints(target: Any, **options: Any) -> dict[str, Any]:
        if target is endpoint:
            raise NameError("class scope shadowed an annotation name")
        return real_get_type_hints(target, **options)

    def get_annotations(_handler: Any, **options: Any) -> dict[str, str]:
        assert options == {"format": "source"}
        return {"value": "int", "return": "str"}

    monkeypatch.setitem(
        handlerhints.__dict__, "sys", SimpleNamespace(version_info=(3, 14))
    )
    monkeypatch.setitem(
        handlerhints.__dict__, "typing", SimpleNamespace(get_type_hints=get_type_hints)
    )
    monkeypatch.setitem(
        handlerhints.__dict__,
        "annotationlib",
        SimpleNamespace(
            Format=SimpleNamespace(STRING="source"),
            get_annotations=get_annotations,
        ),
    )
    assert handlerhints.hints_of(endpoint) == {"value": int, "return": str}


def test_pre_314_hint_resolution_preserves_original_resolution_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def endpoint(value: Any) -> None:
        return None

    def unresolved(_handler: Any) -> dict[str, Any]:
        raise NameError("unknown annotation")

    monkeypatch.setitem(
        handlerhints.__dict__, "sys", SimpleNamespace(version_info=(3, 13))
    )
    monkeypatch.setitem(
        handlerhints.__dict__, "typing", SimpleNamespace(get_type_hints=unresolved)
    )
    with pytest.raises(NameError, match="unknown annotation"):
        handlerhints.hints_of(endpoint)


def test_python_314_annotationlib_is_imported_for_the_compatibility_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    annotationlib = SimpleNamespace(Format=SimpleNamespace(STRING="source"))
    monkeypatch.setitem(sys.modules, "annotationlib", annotationlib)
    monkeypatch.setattr(sys, "version_info", (3, 14))
    importlib.reload(handlerhints)
    assert cast(Any, handlerhints).annotationlib is annotationlib
