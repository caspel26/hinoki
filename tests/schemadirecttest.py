"""Schema instances Rust builds directly match the Python constructor's."""

from typing import Any, ClassVar

import pytest

from hinoki import Schema, field_normalize
from hinoki.errors import ValidationError
from hinoki.schema import direct_construction


class Inner(Schema):
    name: str
    size: int


class Outer(Schema):
    title: str
    inner: Inner
    tags: list[Inner]
    note: str | None = None


class Defaulted(Schema):
    name: str
    label: str = "default"


class Customised(Schema):
    name: str

    def __post_init__(self) -> None:
        Customised.seen.append(self.name)

    seen: ClassVar[list[str]] = []


class Allocated(Schema):
    name: str

    def __new__(cls, **_values: Any) -> "Allocated":
        instance = object.__new__(cls)
        object.__setattr__(instance, "allocated", True)
        return instance


class Normalized(Schema):
    name: str

    @field_normalize("name")
    def lower(self, value: str) -> str:
        return value.lower()


def test_eligibility() -> None:
    assert direct_construction(Inner)
    assert direct_construction(Outer)
    assert not direct_construction(Customised)
    assert not direct_construction(Allocated)
    assert not direct_construction(Normalized)


def test_complete_record_equals_the_constructor() -> None:
    built = Outer.validate(
        {
            "title": "t",
            "inner": {"name": "a", "size": 1},
            "tags": [{"name": "b", "size": 2}],
            "note": "n",
        }
    )
    expected = Outer(
        title="t",
        inner=Inner(name="a", size=1),
        tags=[Inner(name="b", size=2)],
        note="n",
    )
    assert built == expected
    assert type(built.inner) is Inner
    assert built.dump(exclude_unset=True) == expected.dump(exclude_unset=True)


def test_supplied_fields_track_presence() -> None:
    partial = Outer.validate(
        {"title": "t", "inner": {"name": "a", "size": 1}, "tags": []}
    )
    assert partial.dump(exclude_unset=True) == {
        "title": "t",
        "inner": {"name": "a", "size": 1},
        "tags": [],
    }
    complete = Outer.validate(
        {"title": "t", "inner": {"name": "a", "size": 1}, "tags": [], "note": None}
    )
    assert complete.dump(exclude_unset=True)["note"] is None


def test_frozen_and_hashable_behaviour_is_kept() -> None:
    built = Inner.validate({"name": "a", "size": 1})
    with pytest.raises(AttributeError):
        built.name = "b"  # type: ignore[misc]


def test_default_fields_still_take_the_constructor() -> None:
    assert Defaulted.validate({"name": "a"}).label == "default"
    assert Defaulted.validate({"name": "a", "label": "x"}).label == "x"


def test_custom_construction_still_runs() -> None:
    Customised.seen.clear()
    Customised.validate({"name": "a"})
    assert Customised.seen == ["a"]
    assert Allocated.validate({"name": "a"}).allocated  # type: ignore[attr-defined]
    assert Normalized.validate({"name": "ABC"}).name == "abc"


def test_invalid_data_is_still_rejected() -> None:
    with pytest.raises(ValidationError):
        Outer.validate({"title": 1, "inner": {"name": "a", "size": 1}, "tags": []})


def test_complete_records_skip_the_python_constructor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    for cls in (Inner, Defaulted):
        original = cls.__dict__["__hinoki_construct_validated__"].__func__

        def spy(
            c: type[Schema],
            values: dict[str, Any],
            path: str = "",
            _original: Any = original,
        ) -> Any:
            calls.append(c.__name__)
            return _original(c, values, path)

        monkeypatch.setattr(cls, "__hinoki_construct_validated__", classmethod(spy))
    Inner.validate({"name": "a", "size": 1})
    assert calls == []
    Defaulted.validate({"name": "a"})
    assert calls == ["Defaulted"]
