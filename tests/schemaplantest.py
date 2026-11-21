"""Construction converts plain scalar fields without the per-field walk."""

from typing import Any

import pytest

from hinoki import Schema
from hinoki import schema as schema_module
from hinoki.errors import ValidationError


class Plain(Schema):
    status: str
    count: int = 0
    ratio: float = 0.5
    ok: bool = True


class Inner(Schema):
    label: str


class Nested(Schema):
    inner: Inner
    tags: list[str]


@pytest.fixture
def walked(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    original = schema_module.instance_value

    def counting(ftype: Any, value: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["path"])
        return original(ftype, value, **kwargs)

    monkeypatch.setattr(schema_module, "instance_value", counting)
    return seen


def test_plain_scalars_skip_the_generic_field_walk(walked: list[str]) -> None:
    item = Plain(status="ok", count=3)
    assert (item.status, item.count, item.ratio, item.ok) == ("ok", 3, 0.5, True)
    assert walked == []


def test_composite_fields_still_walk_with_their_error_path(walked: list[str]) -> None:
    with pytest.raises(ValidationError) as caught:
        Nested(inner=Inner(label="x"), tags=[1])  # type: ignore[list-item]
    assert walked == ["inner", "tags", "tags[0]"]
    assert caught.value.details[0].path.startswith("tags")


def test_plain_scalars_are_still_rejected_when_wrong() -> None:
    with pytest.raises(ValidationError) as caught:
        Plain(status=5)  # type: ignore[arg-type]
    assert caught.value.details[0].path == "status"
    with pytest.raises(ValidationError):
        Plain(status="ok", count=True)
