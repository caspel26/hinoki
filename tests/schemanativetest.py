"""The native constructor, ``validate``, ``validate_text`` and ``replace`` must
behave exactly like the Python paths they replace."""

from __future__ import annotations

import inspect
import pickle
from typing import Any

import pytest

from hinoki import Field, Schema
from hinoki.errors import ValidationError


class Item(Schema):
    name: str
    price: float
    qty: int = 1
    tags: list[str] = Field(default_factory=list)


class Parent(Schema):
    x: int


class Child(Parent):
    y: int


def test_constructor_stores_values_and_defaults() -> None:
    item = Item(name="a", price=2)
    assert item.price == 2.0
    assert type(item.price) is float
    assert item.qty == 1
    assert item.dump(exclude_unset=True) == {"name": "a", "price": 2.0}


def test_constructor_errors_match_the_python_path() -> None:
    with pytest.raises(TypeError):
        Item(name="a")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        Item(name="a", price=1.0, nope=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError) as caught:
        Item(name=1, price=1.0)  # type: ignore[arg-type]
    assert caught.value.details[0].path == "name"


def test_constructor_signature_survives() -> None:
    assert list(inspect.signature(Item).parameters) == ["name", "price", "qty", "tags"]


def test_default_factory_instances_are_not_shared() -> None:
    first, second = Item(name="a", price=1.0), Item(name="b", price=1.0)
    assert first.tags is not second.tags


def test_validate_builds_and_reports_every_problem() -> None:
    assert Item.validate({"name": "a", "price": 1.5}).qty == 1
    with pytest.raises(ValidationError) as caught:
        Item.validate({"name": 1, "price": "x"})
    assert {error.path for error in caught.value.details} == {"name", "price"}


def test_subclass_does_not_inherit_the_parents_cached_entries() -> None:
    assert Parent.validate({"x": 1}).x == 1
    assert Parent.validate_json(b'{"x": 1}') is not None
    with pytest.raises(ValidationError):
        Child.validate({"x": 1})
    assert Child.validate_json(b'{"x": 1}') is None
    assert Child.validate({"x": 1, "y": 2}).y == 2


def test_user_validate_override_is_kept() -> None:
    class Custom(Schema):
        x: int

        @classmethod
        def validate(cls, data: Any) -> Custom:  # noqa: ARG003
            return cls(x=99)

    assert Custom.validate({"x": 1}).x == 99


def test_validate_text_converts_scalars_natively() -> None:
    class Form(Schema):
        count: int
        ok: bool

    form = Form.validate_text({"count": "2", "ok": "true"})
    assert (form.count, form.ok) == (2, True)
    with pytest.raises(ValidationError):
        Form.validate_text({"count": "x", "ok": "true"})


def test_replace_keeps_supplied_tracking_and_validates() -> None:
    base = Item(name="a", price=1.0)
    other = base.replace(qty=3)
    assert other.qty == 3
    assert other.dump(exclude_unset=True) == {"name": "a", "price": 1.0, "qty": 3}
    assert base.qty == 1
    with pytest.raises(TypeError):
        base.replace(nope=1)
    with pytest.raises(ValidationError):
        base.replace(qty="x")


def test_dump_options_still_checked() -> None:
    item = Item(name="a", price=1.0)
    assert item.dump() == item.dump(exclude=())
    assert "qty" not in item.dump(exclude=["qty"])
    with pytest.raises(TypeError):
        item.dump(exclude=None)  # type: ignore[arg-type]


def test_pickle_round_trip() -> None:
    item = Item(name="a", price=1.0, qty=4)
    assert pickle.loads(pickle.dumps(item)) == item  # noqa: S301 - our own bytes
