"""The native constructor, ``validate``, ``validate_text`` and ``replace`` must
behave exactly like the Python paths they replace."""

from __future__ import annotations

import copy
import inspect
import pickle
from typing import Annotated, Any

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


class Options(Schema):
    page: int = 1
    size: int = 20
    ratio: float = 1


def test_no_keywords_supplies_nothing() -> None:
    assert Options().dump(exclude_unset=True) == {}
    assert Options().replace(size=5).dump(exclude_unset=True) == {"size": 5}
    assert Options(**{}).dump(exclude_unset=True) == {}


def test_omitted_int_default_of_a_float_field_is_a_float_on_every_path() -> None:
    for built in (
        Options(),
        Options.validate({}),
        Options.validate_json(b"{}"),
        Options.validate_json(b'{"page": 2}'),
    ):
        assert built is not None
        assert type(built.ratio) is float
    partial = Options.validate_json(b'{"page": 2}')
    assert partial is not None
    assert partial.dump(exclude_unset=True) == {"page": 2}


def test_native_entries_are_cached_on_the_class() -> None:
    class Cached(Schema):
        x: int

    Cached.validate({"x": 1})
    Cached.validate_json(b'{"x": 1}')
    assert Cached.validate({"x": 1}).x == 1
    assert type(Cached.__dict__["validate"]).__name__ == "Pinned"
    assert type(Cached.__dict__["validate_json"]).__name__ == "Pinned"


def test_validate_json_honours_a_validate_override() -> None:
    class Guarded(Schema):
        x: int

        @classmethod
        def validate(cls, data: Any) -> Guarded:  # noqa: ARG003
            raise ValueError("must run")

    assert Guarded.validate_json(b'{"x": 1}') is None


def test_validate_json_on_generic_or_unresolved_class_does_not_raise() -> None:
    class Late(Schema):
        other: Missing  # type: ignore[name-defined]  # noqa: F821

    assert hasattr(Late, "validate_json")
    assert Late.validate_json(b"{}") is None


class Address(Schema):
    city: str
    zip_code: str = Field(default_factory=lambda: "0", alias="zipCode")


class Person(Schema):
    name: str
    age: Annotated[int, Field(ge=0, le=150)]
    score: float = 0.5
    nick: str | None = None
    home: Address
    others: list[Address] = Field(default_factory=list)
    marks: list[float] = Field(default_factory=list)


def _same(data: object) -> None:
    """The direct path and the general validator must agree on every input."""
    try:
        expected: object = Person._validate_ordinary(data)
    except ValidationError as error:
        expected_details = [(d.path, d.code, d.message) for d in error.details]
        with pytest.raises(ValidationError) as caught:
            Person.validate(data)
        assert [
            (d.path, d.code, d.message) for d in caught.value.details
        ] == expected_details
        return
    got = Person.validate(data)
    assert got == expected
    assert got.dump() == expected.dump()
    assert got.dump(exclude_unset=True) == expected.dump(exclude_unset=True)


@pytest.mark.parametrize(
    "data",
    [
        {"name": "a", "age": 3, "home": {"city": "x"}},
        {"name": "a", "age": 3, "home": {"city": "x", "zipCode": "9"}},
        {"name": "a", "age": 3, "score": 2, "home": {"city": "x"}},
        {"name": "a", "age": 3, "nick": None, "home": {"city": "x"}},
        {"name": "a", "age": 3, "home": {"city": "x"}, "others": [{"city": "y"}]},
        {"name": "a", "age": 3, "home": {"city": "x"}, "marks": [1, 2.5]},
        {"name": "a", "age": -1, "home": {"city": "x"}},
        {"name": "a", "age": True, "home": {"city": "x"}},
        {"name": "a", "age": 3, "home": {"city": 1}},
        {"name": "a", "age": 3, "home": {"city": "x", "zip_code": "9"}},
        {"name": "a", "age": 3, "home": {"city": "x"}, "extra": 1},
        {"name": "a", "home": {"city": "x"}},
        {"name": "a", "age": 3, "home": None},
        {"name": "a", "age": 3, "home": {"city": "x"}, "others": [{"city": 2}]},
        {"name": "a", "age": 3, "home": {"city": "x"}, "marks": ["1"]},
        {"name": "a\x00", "age": 3, "home": {"city": "x"}},
        {"name": "a", "age": 10**30, "home": {"city": "x"}},
        {"name": "a", "age": 3, "score": float("nan"), "home": {"city": "x"}},
        [],
        None,
    ],
)
def test_direct_validate_agrees_with_the_general_validator(data: object) -> None:
    _same(data)


def test_direct_validate_keeps_the_input_untouched() -> None:
    data = {"name": "a", "age": 3, "home": {"city": "x"}, "marks": [1, 2]}
    built = Person.validate(data)
    assert data == {"name": "a", "age": 3, "home": {"city": "x"}, "marks": [1, 2]}
    assert built.marks == [1.0, 2.0]
    assert built.home is not data["home"]


class Basket(Schema):
    owner: Person
    items: list[Item]


def _basket() -> Basket:
    return Basket(
        owner=Person(name="a", age=3, home=Address(city="x")),
        items=[Item(name="n", price=1.5, tags=["t"])],
    )


def test_copy_keeps_values_and_supplied_fields() -> None:
    built = Item(name="n", price=1.0)
    twin = copy.copy(built)
    assert twin == built
    assert twin is not built
    assert twin.dump(exclude_unset=True) == built.dump(exclude_unset=True)


def test_deepcopy_copies_what_is_mutable() -> None:
    built = _basket()
    twin = copy.deepcopy(built)
    assert twin == built
    assert twin.items is not built.items
    assert twin.items[0].tags is not built.items[0].tags
    assert twin.dump(exclude_unset=True) == built.dump(exclude_unset=True)


def test_deepcopy_shares_a_value_seen_twice() -> None:
    shared = Item(name="n", price=1.0)
    twin = copy.deepcopy([shared, shared])
    assert twin[0] is twin[1]


def test_pickle_round_trips_nested_schemas() -> None:
    built = _basket()
    for protocol in range(2, pickle.HIGHEST_PROTOCOL + 1):
        again = pickle.loads(pickle.dumps(built, protocol))  # noqa: S301 - our own bytes
        assert again == built
        assert again.dump(exclude_unset=True) == built.dump(exclude_unset=True)


def _rebuild(cls: type[Schema], values: dict[str, Any]) -> Schema:
    return cls(**values)


def test_a_class_with_its_own_copy_protocol_keeps_it() -> None:
    class Marked(Schema):
        x: int

        def __copy__(self) -> Marked:
            return Marked(x=99)

        def __reduce__(self) -> tuple[Any, ...]:
            return (_rebuild, (Marked, {"x": self.x}))

    assert copy.copy(Marked(x=1)).x == 99
    assert copy.deepcopy(Marked(x=1)) == Marked(x=1)


def test_native_protocol_methods_follow_subclasses() -> None:
    class Wide(Item):
        extra: int = 0

    built = Wide(name="n", price=1.0, extra=4)
    assert copy.copy(built) == built
    assert copy.deepcopy(built).extra == 4


class Inner(Schema):
    x: int


class Outer(Schema):
    inner: Inner
    nums: list[int] = Field(default_factory=list)
    items: list[Inner] = Field(default_factory=list)


def test_validate_refuses_ready_instances_like_the_ordinary_path() -> None:
    for data in (
        {"inner": Inner(x=1)},
        {"inner": {"x": 1}, "items": [Inner(x=2)]},
    ):
        with pytest.raises(ValidationError):
            Outer._validate_ordinary(data)
        with pytest.raises(ValidationError):
            Outer.validate(data)


def test_validate_does_not_keep_the_input_list() -> None:
    nums = [1, 2]
    built = Outer.validate({"inner": {"x": 1}, "nums": nums})
    nums.append(3)
    assert built.nums == [1, 2]


def test_user_protocol_on_a_subclass_replaces_the_inherited_native_one() -> None:
    class Base(Schema):
        a: int

    class Child(Base):
        b: int = 0

        def __getstate__(self) -> dict[str, int]:
            return {"a": self.a, "b": self.b}

    built = Child(a=1, b=2)
    assert copy.copy(built).b == 2
    assert copy.deepcopy(built).b == 2


def test_a_user_reduce_is_used_by_copy() -> None:
    calls: list[int] = []

    class Money(Schema):
        cents: int

        def __reduce__(self) -> tuple[Any, ...]:
            calls.append(self.cents)
            return (_rebuild, (Money, {"cents": self.cents}))

    copy.copy(Money(cents=5))
    assert calls == [5]


def test_super_validate_builds_the_calling_subclass() -> None:
    class Parent2(Schema):
        a: int

    class Child2(Parent2):
        b: int = 0

        @classmethod
        def validate(cls, data: object) -> Any:
            return super().validate(data)

    Parent2.validate({"a": 1})
    assert type(Child2.validate({"a": 1, "b": 2})) is Child2
