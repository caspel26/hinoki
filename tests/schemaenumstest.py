"""``Literal`` and ``Enum`` Schema fields: registration, wire forms, every
refusal with its code and path, construction, output and OpenAPI."""

from __future__ import annotations

import enum
from typing import Annotated, Any, Literal

import pytest
from openapi_spec_validator import validate

from hinoki import UNSET, Field, Hinoki, Schema, Unset, core, http
from hinoki.errors import RegistrationError, ValidationError
from hinoki.orm import Model, field
from hinoki.rules import Project, RuleSet, Schemas
from hinoki.schema import encode
from hinoki.testing import ASGIClient


class Colour(enum.StrEnum):
    RED = "red"
    GREEN = "green"


class Level(enum.IntEnum):
    LOW = 1
    HIGH = 3


class Plain(enum.Enum):
    SMALL = "s"
    LARGE = "l"


class Number(enum.Enum):
    ONE = 1
    TWO = 2


class Paint(Schema):
    colour: Colour
    level: Level
    plain: Plain
    number: Number
    mode: Literal["fast", "slow"]
    size: Literal[1, 2, 3]


def problems(cls: type[Schema], data: object) -> list[tuple[str, str, str]]:
    with pytest.raises(ValidationError) as caught:
        cls.validate(data)
    return [(d.path, d.code, d.message) for d in caught.value.details]


def valid(**changes: object) -> dict[str, object]:
    data: dict[str, object] = {
        "colour": "red",
        "level": 3,
        "plain": "l",
        "number": 2,
        "mode": "slow",
        "size": 2,
    }
    return {**data, **changes}


def test_enums_and_literals_register_as_their_value_kind() -> None:
    kinds = {name: spec.ftype for name, spec in Paint.specs().items()}
    assert {name: kind.kind for name, kind in kinds.items()} == {
        "colour": "str",
        "level": "int",
        "plain": "str",
        "number": "int",
        "mode": "str",
        "size": "int",
    }
    assert kinds["colour"].info.one_of == ("red", "green")
    assert kinds["level"].info.one_of == (1, 3)
    assert kinds["size"].info.one_of == (1, 2, 3)


def test_valid_input_becomes_members_and_literal_values() -> None:
    paint = Paint.validate(valid())
    assert paint.colour is Colour.RED
    assert paint.level is Level.HIGH
    assert paint.plain is Plain.LARGE
    assert paint.number is Number.TWO
    assert (paint.mode, paint.size) == ("slow", 2)


@pytest.mark.parametrize(
    ("field_name", "bad", "code"),
    [
        ("colour", "blue", "one_of"),
        ("colour", "RED", "one_of"),
        ("colour", "", "one_of"),
        ("colour", 1, "type"),
        ("colour", None, "null"),
        ("colour", ["red"], "type"),
        ("level", 2, "one_of"),
        ("level", "3", "type"),
        ("level", 3.0, "type"),
        ("level", True, "type"),
        ("plain", "x", "one_of"),
        ("plain", 1, "type"),
        ("number", 3, "one_of"),
        ("number", "1", "type"),
        ("mode", "medium", "one_of"),
        ("mode", None, "null"),
        ("size", 4, "one_of"),
        ("size", True, "type"),
        ("size", "1", "type"),
    ],
)
def test_values_outside_the_declared_set_are_refused(
    field_name: str, bad: object, code: str
) -> None:
    found = problems(Paint, valid(**{field_name: bad}))
    assert [(path, c) for path, c, _ in found] == [(field_name, code)]


def test_the_message_lists_the_values_not_the_member_names() -> None:
    assert problems(Paint, valid(colour="blue")) == [
        ("colour", "one_of", "must be one of 'red', 'green'")
    ]
    assert problems(Paint, valid(level=2)) == [
        ("level", "one_of", "must be one of 1, 3")
    ]


def test_a_str_member_is_read_as_its_text() -> None:
    assert Paint.validate(valid(colour=Colour.GREEN)).colour is Colour.GREEN


def test_a_member_of_another_enum_is_refused() -> None:
    assert [c for _, c, _ in problems(Paint, valid(colour=Plain.SMALL))] == ["type"]


class Optional(Schema):
    colour: Colour | None = None
    level: Level | Unset = UNSET
    colours: list[Colour] = Field(default_factory=list)
    levels: dict[str, Level] = Field(default_factory=dict)
    mode: Literal["a", "b"] | None = "a"


def test_nullable_unset_defaults_lists_and_dicts() -> None:
    empty = Optional.validate({})
    assert (empty.colour, empty.level, empty.colours, empty.mode) == (
        None,
        UNSET,
        [],
        "a",
    )
    full = Optional.validate(
        {
            "colour": "green",
            "level": 1,
            "colours": ["red", "green"],
            "levels": {"x": 3},
            "mode": None,
        }
    )
    assert full.colour is Colour.GREEN
    assert full.level is Level.LOW
    assert full.colours == [Colour.RED, Colour.GREEN]
    assert full.levels == {"x": Level.HIGH}
    assert full.mode is None


def test_errors_inside_containers_have_full_paths() -> None:
    found = problems(
        Optional, {"colours": ["red", "pink"], "levels": {"x": 2}, "level": "1"}
    )
    assert [(path, code) for path, code, _ in found] == [
        ("level", "type"),
        ("colours[1]", "one_of"),
        ("levels.x", "one_of"),
    ]


class Defaults(Schema):
    colour: Colour = Colour.RED
    level: Level = Level.LOW
    mode: Literal["a", "b"] = "b"


def test_defaults_are_members_and_literal_values() -> None:
    made = Defaults()
    assert (made.colour, made.level, made.mode) == (Colour.RED, Level.LOW, "b")
    assert Defaults.validate({}).colour is Colour.RED


def test_construction_takes_members_and_listed_values_only() -> None:
    made = Paint(
        colour=Colour.GREEN,
        level=Level.LOW,
        plain=Plain.SMALL,
        number=Number.ONE,
        mode="fast",
        size=1,
    )
    assert made.plain is Plain.SMALL
    for changes in (
        {"colour": "green"},
        {"level": 1},
        {"plain": "s"},
        {"mode": "medium"},
        {"size": 4},
    ):
        data: dict[str, Any] = {
            "colour": Colour.RED,
            "level": Level.LOW,
            "plain": Plain.SMALL,
            "number": Number.ONE,
            "mode": "fast",
            "size": 1,
            **changes,
        }
        with pytest.raises(ValidationError):
            Paint(**data)


def test_output_writes_the_value() -> None:
    paint = Paint.validate(valid())
    expected = {
        "colour": "red",
        "level": 3,
        "plain": "l",
        "number": 2,
        "mode": "slow",
        "size": 2,
    }
    assert paint.dump() == expected
    assert paint.to_dict() == {
        **expected,
        "colour": Colour.RED,
        "level": Level.HIGH,
        "plain": Plain.LARGE,
        "number": Number.TWO,
    }
    assert encode(paint) == expected
    assert type(paint.dump()["plain"]) is str
    assert type(paint.dump()["level"]) is int
    assert Paint.validate(paint.dump()) == paint


def test_the_json_encoder_writes_members_inside_schemas() -> None:
    paint = Paint.validate(valid())
    text = bytes(core.encode_json(paint)).decode()
    assert '"plain":"l"' in text
    assert '"number":2' in text
    assert '"colour":"red"' in text
    assert '"level":3' in text
    assert core.encode_json({"plain": Plain.SMALL}) == b'{"plain":"s"}'


@pytest.mark.parametrize(
    ("annotation", "message"),
    [
        (Literal["a", 1], "mixes"),
        (Literal["a", None], "None"),
        (Literal[True], "bool"),
        (Literal[b"x"], "str or int"),
        (Literal[2**70], "64-bit"),
        (Literal[Colour.RED], "enum"),
    ],
)
def test_unsupported_literals_fail_at_class_creation(
    annotation: Any, message: str
) -> None:
    with pytest.raises(RegistrationError, match=message):
        type("Bad", (Schema,), {"__annotations__": {"value": annotation}})


class Mixed(enum.Enum):
    A = "a"
    B = 1


class Flags(enum.IntFlag):
    A = 1
    B = 2


class Empty(enum.Enum):
    pass


class Real(enum.Enum):
    A = 1.5


class Booly(enum.Enum):
    A = True


class Huge(enum.Enum):
    A = 2**70


class Tuple(enum.Enum):
    A = (1, 2)


@pytest.mark.parametrize(
    ("annotation", "message"),
    [
        (Mixed, "mixes"),
        (Flags, "Flag"),
        (Empty, "no members"),
        (Real, "str or int"),
        (Booly, "str or int"),
        (Huge, "64-bit"),
        (Tuple, "str or int"),
    ],
)
def test_unsupported_enums_fail_at_class_creation(
    annotation: Any, message: str
) -> None:
    with pytest.raises(RegistrationError, match=message):
        type("Bad", (Schema,), {"__annotations__": {"value": annotation}})


@pytest.mark.parametrize(
    "info",
    [
        Field(one_of=["red"]),
        Field(min_length=1),
        Field(pattern="r.*"),
    ],
)
def test_a_listed_type_refuses_constraints(info: Any) -> None:
    with pytest.raises(RegistrationError, match="lists its own values"):
        type(
            "Bad",
            (Schema,),
            {"__annotations__": {"value": Annotated[Colour, info]}},
        )


def test_an_int_literal_refuses_bounds() -> None:
    with pytest.raises(RegistrationError, match="lists its own values"):
        type(
            "Bad",
            (Schema,),
            {"__annotations__": {"value": Annotated[Literal[1, 2], Field(ge=1)]}},
        )


class Twice(enum.Enum):
    A = "a"
    B = "a"


class Ticket(Model, table="enum_tickets"):
    id: int = field(primary_key=True)
    colour: Colour


def test_an_enum_column_is_read_as_the_enum_and_a_literal_column_is_refused() -> None:
    colour = Ticket.__field_types__["colour"]
    assert colour.enum_class is Colour
    with pytest.raises(RegistrationError, match=r"Moded\.mode: no column type"):

        class Moded(Model, table="enum_moded"):
            id: int = field(primary_key=True)
            mode: Literal["a", "b"] | None = None


class Holder(Schema):
    value: Twice


def test_enum_aliases_are_one_choice() -> None:
    assert Holder.specs()["value"].ftype.info.one_of == ("a",)
    assert Holder.validate({"value": "a"}).value is Twice.A


def test_a_str_enum_keeps_str_subclass_behaviour() -> None:
    assert Paint.validate(valid()).colour == "red"


@http.post("/enums/paint")
async def echo_paint(data: Paint) -> Paint:
    assert data.colour is Colour.RED
    return data


@http.post("/enums/optional")
def echo_optional(data: Optional) -> Optional:
    return data


def test_openapi_lists_the_values() -> None:
    app = Hinoki()
    app.include(echo_paint, echo_optional)
    document = app.openapi()
    validate(document)
    schemas = document["components"]["schemas"]
    properties = schemas["Paint"]["properties"]
    assert properties["colour"] == {"type": "string", "enum": ["red", "green"]}
    assert properties["level"] == {"type": "integer", "enum": [1, 3]}
    assert properties["plain"] == {"type": "string", "enum": ["s", "l"]}
    assert properties["number"] == {"type": "integer", "enum": [1, 2]}
    assert properties["mode"] == {"type": "string", "enum": ["fast", "slow"]}
    assert properties["size"] == {"type": "integer", "enum": [1, 2, 3]}
    nullable = schemas["Optional"]["properties"]
    assert nullable["colour"] == {
        "anyOf": [{"type": "string", "enum": ["red", "green"]}, {"type": "null"}]
    }
    assert nullable["colours"]["items"] == {"type": "string", "enum": ["red", "green"]}


async def test_request_bodies_and_responses_use_the_values() -> None:
    app = Hinoki()
    app.include(echo_paint)
    async with ASGIClient(app) as client:
        reply = await client.post("/enums/paint", json=valid())
        assert reply.status == 200
        assert reply.body == valid()
        reply = await client.post("/enums/paint", json=valid(colour="blue", size=9))
        assert reply.status == 422
        assert [(d["path"], d["code"]) for d in reply.body["details"]] == [
            ("data.colour", "one_of"),
            ("data.size", "one_of"),
        ]


class Picks(Schema):
    colour: Colour
    mode: Literal["a", "b"]
    note: Annotated[str, Field(max_length=10)]


@http.post("/enums/picks")
def picks(data: Picks) -> dict[str, int]:
    return {}


def test_string_limits_rule_accepts_listed_values() -> None:
    app = Hinoki()
    app.include(picks)
    assert RuleSet(Schemas.require_string_limits()).run(Project(app)) == []
