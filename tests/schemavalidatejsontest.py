"""``Schema.validate_json``: the one-pass typed decode agrees with ``validate``
on everything it accepts and steps aside, for the ordinary path to report,
on everything else."""

import copy
import json
import pickle
import random
import types
from typing import Annotated

import pytest

from hinoki import Field, Invalid, Schema, core, field_check
from hinoki.errors import RegistrationError, ValidationError


class Item(Schema):
    sku: Annotated[str, Field(min_length=2, max_length=8)]
    qty: Annotated[int, Field(ge=0, le=1000)]


class Order(Schema):
    title: Annotated[str, Field(alias="orderTitle")]
    price: float
    paid: bool
    note: str | None = None
    labels: list[str] = Field(default_factory=list)
    items: list[Item]
    first: Item


class Strict(Schema):
    name: str


class Checked(Schema):
    name: str

    @field_check("name")
    def not_blank(self, value: str) -> None:
        if not value.strip():
            raise Invalid("blank", "name is blank")


GOOD = {
    "orderTitle": 'Tea \u00e9\U0001f375 "quoted"',
    "price": 3,
    "paid": True,
    "items": [{"sku": "ab", "qty": 1}, {"sku": "cdef", "qty": 1000}],
    "first": {"sku": "zz", "qty": 0},
}


def both(cls: type[Schema], text: str) -> tuple[object, object]:
    raw = text.encode()
    return cls.validate_json(raw), cls.validate(core.decode_json(raw))


def test_accepted_documents_match_the_ordinary_path() -> None:
    for extra in (
        {},
        {"note": None},
        {"note": "n", "labels": ["a", "b"]},
        {"price": 2.5},
    ):
        typed, plain = both(Order, json.dumps({**GOOD, **extra}))
        assert typed is not None
        assert typed == plain
        assert type(typed) is Order
        assert isinstance(typed.price, float)
        assert type(typed.first) is Item


def test_each_refusal_is_left_to_the_ordinary_path() -> None:
    cases = [
        "{",
        json.dumps({**GOOD, "items": [{"sku": "a", "qty": 1}]}),
        json.dumps({**GOOD, "paid": 1}),
        json.dumps({**GOOD, "price": "3"}),
        json.dumps({k: v for k, v in GOOD.items() if k != "first"}),
        json.dumps({**GOOD, "orderTitle": "a\u0000b"}),
    ]
    for text in cases:
        assert Order.validate_json(text.encode()) is None
    with pytest.raises(ValidationError):
        Order.validate(core.decode_json(cases[1].encode()))


def test_duplicate_keys_are_left_to_the_ordinary_path() -> None:
    typed, plain = both(Strict, '{"name": "a", "name": "b"}')
    assert typed is None
    assert plain.name == "b"  # type: ignore[attr-defined]


def test_checks_keep_the_ordinary_path() -> None:
    assert Checked.validate_json(b'{"name": "x"}') is None
    assert Checked.validate(core.decode_json(b'{"name": "x"}')).name == "x"


def test_unknown_keys_bounds_and_big_numbers() -> None:
    typed, plain = both(Item, '{"sku": "ab", "qty": 3}')
    assert typed == plain
    for text in (
        '{"sku": "ab", "qty": 3, "extra": {"a": [1, 2.5, null, "x"]}}',
        '{"sku": "ab", "qty": 1001}',
        '{"sku": "ab", "qty": 99999999999999999999}',
        '{"sku": "ab", "qty": 1.5}',
        '{"sku": "ab", "qty": 1, "extra": [1,]}',
        '{"sku": "ab\\ud800", "qty": 1}',
    ):
        assert Item.validate_json(text.encode()) is None


def test_depth_and_invalid_utf8_are_left_to_the_ordinary_path() -> None:
    assert Item.validate_json(b'{"sku": "a\xff", "qty": 1}') is None
    deep = b'{"sku": "ab", "qty": 1, "x": ' + b"[" * 200 + b"]" * 200 + b"}"
    assert Item.validate_json(deep) is None


def test_random_mutations_never_disagree_with_the_ordinary_path() -> None:
    rng = random.Random(7)  # noqa: S311 - a seeded mutation, not a secret
    base = json.dumps({**GOOD, "note": "n", "labels": ["a"]}).encode()
    noise = b'{}[]",:\\ \x00\xffe-0123456789.truefalsnl'
    for _ in range(4000):
        raw = bytearray(base)
        for _ in range(rng.randint(1, 4)):
            at = rng.randrange(len(raw))
            match rng.randrange(3):
                case 0:
                    raw[at] = rng.choice(noise)
                case 1:
                    del raw[at]
                case _:
                    raw.insert(at, rng.choice(noise))
        data = bytes(raw)
        typed = Order.validate_json(data)
        if typed is None:
            continue
        assert typed == Order.validate(core.decode_json(data))


class Base(Schema):
    code: str
    size: int = 3


class Child(Base):
    extra: float = 1.5
    _private: int = 0


class MixinA(Schema, slots=False):
    left: int


class MixinB(Schema):
    right: int


class Both(MixinA, MixinB):
    pass


def test_fields_live_in_slots_and_keep_defaults_and_inheritance() -> None:
    typed = Child.validate_json(b'{"code": "a"}')
    assert typed is not None
    assert typed == Child.validate({"code": "a"})
    assert (typed.code, typed.size, typed.extra) == ("a", 3, 1.5)
    assert "code" not in vars(typed)
    assert copy.copy(typed) == typed
    assert pickle.loads(pickle.dumps(typed)) == typed  # noqa: S301


def test_a_second_field_bearing_base_needs_slots_false() -> None:
    both = Both.validate({"left": 1, "right": 2})
    assert (both.left, both.right) == (1, 2)
    assert Both.validate_json(b'{"left": 1, "right": 2}') in (None, both)
    with pytest.raises(RegistrationError, match="slots=False"):

        class Clash(MixinB, Base):
            pass


def test_a_schema_of_a_hundred_fields_agrees_with_validate() -> None:
    wide: type[Schema] = types.new_class(
        "Wide",
        (Schema,),
        {},
        lambda ns: ns.update(__annotations__={f"f{i}": int for i in range(100)}),
    )
    body = {f"f{i}": i for i in range(100)}
    raw = json.dumps(body).encode()
    assert wide.validate_json(raw) == wide.validate(body)
    del body["f57"]
    assert wide.validate_json(json.dumps(body).encode()) is None


@pytest.mark.parametrize(
    "text",
    [
        "0.1",
        "-0.0",
        "0.30000000000000004",
        "123456.789",
        "9007199254740992.0",
        "9007199254740993.5",
        "1.5e3",
        "0.000000000000000000001",
        "-12.625",
        "3.14159265358979323846",
        "1.0000000000000000000",
        "100000000000000000000.5",
    ],
)
def test_floats_agree_with_the_ordinary_parse(text: str) -> None:
    class Reading(Schema):
        value: float

    raw = f'{{"value": {text}}}'.encode()
    typed = Reading.validate_json(raw)
    if typed is not None:
        assert repr(typed.value) == repr(Reading.validate(json.loads(raw)).value)


@pytest.mark.parametrize("pad", range(0, 18))
@pytest.mark.parametrize(
    "special", ['"', "\\", "\n", "\x1f", "\u00e9", "\U0001f375", "/"]
)
def test_strings_agree_whatever_their_alignment(pad: int, special: str) -> None:
    text = "a" * pad + special + "b" * pad
    raw = json.dumps({"name": text}).encode()
    typed = Strict.validate_json(raw)
    assert typed is not None
    assert typed == Strict.validate({"name": text})
    ctl = f'{{"name": "{"a" * pad}\x01"}}'.encode()
    assert Strict.validate_json(ctl) is None
