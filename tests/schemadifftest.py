"""The Rust validator must agree with the pure-Python oracle on every input."""

import datetime as dt
import enum
import math
import random
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import pytest

from hinoki import UNSET, Field, Schema, SchemaConfig, SchemaExtra, Unset
from hinoki.errors import ErrorDetail, ValidationError
from hinoki.fieldtypes import FieldType
from tests.oracle.validation import INVALID, TooManyErrors, Validator


class Point(Schema):
    x: float
    y: Annotated[float, Field(ge=-10, lt=10.5)]


class Tag(Schema):
    label: Annotated[str, Field(min_length=1, max_length=5)]
    weight: Annotated[int, Field(gt=0, le=100)] = 1


class Post(Schema):
    id: UUID
    title: Annotated[str, Field(min_length=2, max_length=20, pattern=r"[A-Za-z ]+")]
    published: bool
    created: datetime
    rank: int | None = None
    note: str | Unset = UNSET
    tags: Annotated[list[Tag], Field(max_length=3)]
    origin: Point | None = None
    scores: list[float] = Field(default_factory=list)


class Node(Schema):
    name: str
    children: list["Node"] = Field(default_factory=list)


class ExtensiblePayload(Schema, config=SchemaConfig(extra=SchemaExtra.IGNORE)):
    count: int


class LengthMessages(Schema):
    name: Annotated[str, Field(min_length=2, max_length=4)]
    tags: Annotated[list[str], Field(min_length=1, max_length=2)]


class Mapping(Schema):
    labels: Annotated[
        dict[str, Annotated[str, Field(max_length=3)]], Field(max_length=3)
    ]
    points: dict[Annotated[str, Field(min_length=2, pattern="[a-z]+")], Point] = Field(
        default_factory=dict
    )
    grid: dict[str, list[int] | None] | None = None


def native(cls: type[Schema], data: object) -> Any:
    return cls.validate(data)


def oracle(cls: type[Schema], data: object) -> Any:
    """The old ``Schema.validate``, kept as the reference implementation."""
    validator = Validator()
    try:
        value = validator.schema(FieldType("schema", schema=cls), data, "")
    except TooManyErrors:
        validator.errors.append(
            ErrorDetail("", "too_many_errors", "further errors omitted")
        )
        raise ValidationError(validator.errors) from None
    if value is INVALID:
        raise ValidationError(validator.errors)
    return value


def outcome(call: Any, cls: type[Schema], data: object) -> tuple[str, Any]:
    try:
        return "ok", call(cls, data)
    except ValidationError as error:
        return "errors", [(d.path, d.code, d.message) for d in error.details]


def test_native_and_oracle_agree_on_ignored_extra_policy() -> None:
    inputs = [
        {"count": 1, "unknown": {"unvisited": object()}},
        {"count": "1", "unknown": "discarded"},
        {"count": 1, 7: "non-string key"},
        {7: "non-string key"},
        {"count": 1, "unknown": "discarded", 7: "non-string key"},
    ]
    for data in inputs:
        assert outcome(native, ExtensiblePayload, data) == outcome(
            oracle, ExtensiblePayload, data
        )


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (
            {"name": "x", "tags": ["one"]},
            ("name", "min_length", "at least 2 characters"),
        ),
        (
            {"name": "longer", "tags": ["one"]},
            ("name", "max_length", "at most 4 characters"),
        ),
        ({"name": "ok", "tags": []}, ("tags", "min_length", "at least 1 item")),
        (
            {"name": "ok", "tags": ["a", "b", "c"]},
            ("tags", "max_length", "at most 2 items"),
        ),
    ],
)
def test_length_error_messages_name_the_unit(
    data: dict[str, Any], expected: tuple[str, str, str]
) -> None:
    assert outcome(native, LengthMessages, data) == outcome(
        oracle, LengthMessages, data
    )
    with pytest.raises(ValidationError) as caught:
        native(LengthMessages, data)
    detail = caught.value.details[0]
    assert (detail.path, detail.code, detail.message) == expected


JUNK: list[Any] = [
    None, True, False, 0, -1, 7, 10**30, -(10**30), 1.5, -0.0, float("inf"),
    float("-inf"), float("nan"), "", " ", "x", "x" * 70, "é", "Ünï", [], {}, [1],
    {"a": 1}, "not-a-uuid", "12345678-1234-5678-1234-567812345678",
    "12345678123456781234567812345678", "{12345678-1234-5678-1234-567812345678}",
    "2026-11-13T10:00:00", "2026-11-13T10:00:00Z", "2026-11-13T10:00:00+01:00",
    "2026-11-13", "2026-13-45T99:99:99Z", "2026-11-13T10:00:00.5-05:30",
    "2026-11-13 10:00:00Z", "２０２６-11-13T10:00:00Z",
]  # fmt: skip


def valid_post(rng: random.Random) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": str(uuid4()),
        "title": "Hello World",
        "published": rng.random() < 0.5,
        "created": "2026-11-13T10:00:00Z",
        "tags": [{"label": "ab", "weight": 5} for _ in range(rng.randint(0, 3))],
    }
    if rng.random() < 0.5:
        data["rank"] = rng.choice([None, 1, 2])
    if rng.random() < 0.3:
        data["note"] = "n"
    if rng.random() < 0.4:
        data["origin"] = {"x": rng.random(), "y": rng.uniform(-10, 10)}
    if rng.random() < 0.4:
        data["scores"] = [rng.random() for _ in range(rng.randint(0, 3))]
    return data


def mutate(rng: random.Random, value: Any, depth: int = 0) -> Any:
    """Copy ``value`` replacing, adding or dropping parts with junk."""
    roll = rng.random()
    if roll < 0.12 and depth > 0:
        return rng.choice(JUNK)
    if isinstance(value, dict):
        out = {
            k: mutate(rng, v, depth + 1)
            for k, v in value.items()
            if rng.random() > 0.05
        }
        if rng.random() < 0.08:
            out[rng.choice(["extra", "x" * 80, "é"])] = rng.choice(JUNK)
        return out
    if isinstance(value, list):
        out_list = [mutate(rng, v, depth + 1) for v in value]
        if rng.random() < 0.1:
            out_list.append(rng.choice(JUNK))
        return out_list
    return value


def tree(rng: random.Random, depth: int) -> dict[str, Any]:
    kids = [tree(rng, depth - 1) for _ in range(rng.randint(0, 2))] if depth else []
    return {"name": "n", "children": kids}


@pytest.mark.parametrize("seed", range(8))
def test_post_inputs_agree(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(600):
        data = mutate(rng, valid_post(rng))
        assert outcome(native, Post, data) == outcome(oracle, Post, data), data


@pytest.mark.parametrize("seed", range(4))
def test_recursive_inputs_agree(seed: int) -> None:
    rng = random.Random(100 + seed)
    for _ in range(300):
        data = mutate(rng, tree(rng, rng.randint(0, 4)))
        assert outcome(native, Node, data) == outcome(oracle, Node, data), data


@pytest.mark.parametrize("junk", JUNK)
def test_every_junk_value_in_every_slot_agrees(junk: Any) -> None:
    base = {
        "id": str(uuid4()), "title": "Hello", "published": True,
        "created": "2026-11-13T10:00:00Z", "tags": [{"label": "a"}],
        "origin": {"x": 1.0, "y": 2.0}, "scores": [1.0],
    }  # fmt: skip
    assert outcome(native, Post, junk) == outcome(oracle, Post, junk)
    for key in list(base):
        data = {**base, key: junk}
        assert outcome(native, Post, data) == outcome(oracle, Post, data), (
            key,
            junk,
        )
    for path in (
        ("tags", 0, "label"),
        ("tags", 0, "weight"),
        ("origin", "y"),
        ("scores", 0),
    ):
        data = {
            **base,
            "tags": [{"label": "a"}],
            "origin": {"x": 1.0, "y": 2.0},
            "scores": [1.0],
        }
        target: Any = data
        for step in path[:-1]:
            target = target[step]
        target[path[-1]] = junk
        assert outcome(native, Post, data) == outcome(oracle, Post, data), (
            path,
            junk,
        )


def valid_mapping(rng: random.Random) -> dict[str, Any]:
    data: dict[str, Any] = {
        "labels": {rng.choice(["a", "b", "é", "__proto__"]): "x" for _ in range(2)}
    }
    if rng.random() < 0.6:
        data["points"] = {"ab": {"x": 1.0, "y": 2.0}, "zz": {"x": 0.5, "y": 0.0}}
    if rng.random() < 0.6:
        data["grid"] = {"row": [1, 2], "none": None}
    return data


@pytest.mark.parametrize("seed", range(6))
def test_mapping_inputs_agree(seed: int) -> None:
    rng = random.Random(500 + seed)
    for _ in range(600):
        data = mutate(rng, valid_mapping(rng))
        assert outcome(native, Mapping, data) == outcome(oracle, Mapping, data), data


@pytest.mark.parametrize("junk", JUNK)
def test_every_junk_value_in_every_mapping_slot_agrees(junk: Any) -> None:
    base = {
        "labels": {"a": "x"},
        "points": {"ab": {"x": 1.0, "y": 2.0}},
        "grid": {"row": [1]},
    }
    for key in list(base):
        data = {**base, key: junk}
        assert outcome(native, Mapping, data) == outcome(oracle, Mapping, data)
    for field_name, entry in (("labels", "a"), ("points", "ab"), ("grid", "row")):
        data = {**base, field_name: {entry: junk}}
        assert outcome(native, Mapping, data) == outcome(oracle, Mapping, data)
    for bad_key in ("", "A", "x" * 70, 7):
        data = {**base, "points": {bad_key: {"x": 1.0, "y": 2.0}}}
        assert outcome(native, Mapping, data) == outcome(oracle, Mapping, data)


def test_many_problems_are_capped_the_same_way() -> None:
    data = {
        "tags": [{"label": 1} for _ in range(3)],
        "scores": ["x"] * 80,
        **{f"e{i}": 1 for i in range(60)},
    }
    native_result = outcome(native, Post, data)
    assert native_result == outcome(oracle, Post, data)
    assert native_result[1][-1][1] == "too_many_errors" and len(native_result[1]) == 51


def test_key_echo_and_unicode_lengths_agree() -> None:
    data = {"title": "é" * 25, "x" * 100: 1, "ü" * 100: 2}
    assert outcome(native, Post, data) == outcome(oracle, Post, data)


def test_results_are_equal_instances() -> None:
    rng = random.Random(7)
    data = valid_post(rng)
    first = Post.validate(data)
    second = oracle(Post, data)
    assert first == second and isinstance(
        first.tags[0] if first.tags else Tag(label="a"), Tag
    )
    assert not math.isnan(first.scores[0]) if first.scores else True


class Choices(Schema):
    colour: Annotated[str, Field(one_of=["red", "green", "blue"])]
    size: Annotated[int, Field(one_of=[1, 2, 3])]
    ratio: Annotated[float, Field(one_of=[0.5, 1])] | None = None


@pytest.mark.parametrize(
    "data",
    [
        {"colour": "red", "size": 1},
        {"colour": "pink", "size": 1},
        {"colour": "red", "size": 9, "ratio": 0.5},
        {"colour": "red", "size": True},
        {"colour": 5, "size": "1"},
        {"colour": "red", "size": 2, "ratio": 1},
        {"colour": "red", "size": 2, "ratio": 1.0},
        {"colour": "red", "size": 2, "ratio": 2},
        {"colour": "red", "size": 2, "ratio": None},
        {},
        None,
    ],
)
def test_one_of_agrees_with_the_oracle(data: Any) -> None:
    assert outcome(native, Choices, data) == outcome(oracle, Choices, data)


class Wire(Schema):
    day: dt.date
    at: dt.time | None = None
    wait: dt.timedelta | Unset = UNSET
    amount: Annotated[
        Decimal, Field(ge=Decimal("-100.5"), lt=1000, max_digits=6, decimal_places=2)
    ] = Decimal(0)
    loose: Decimal | None = None
    blob: Annotated[bytes, Field(min_length=1, max_length=6)] = b"x"
    blobs: list[bytes] = Field(default_factory=list)
    stamps: dict[str, dt.date] = Field(default_factory=dict)


WIRE_JUNK: list[Any] = [
    *JUNK, "2026-11-18", "2026-02-29", "0000-01-01", "2026-11-18T00:00:00Z",
    "09:30:00Z", "09:30:00", "24:00:00Z", "09:30:00.1234567Z", "09:30:00+24:00",
    "P1D", "PT0.5S", "-P1DT1S", "P", "PT", "P1W", "P1000000000D", "PT1.5H",
    "12.50", "-0", "0.001", "1e5", "NaN", "01", "1" * 70, 10**70, "999.99",
    "-100.5", "-100.51", "1000", "AA==", "QR==", "QQ", "QUJDREVG", "QUJDREVGRw==",
    "", "-_-_", "١", "１", "١٢",
]  # fmt: skip


def valid_wire(rng: random.Random) -> dict[str, Any]:
    data: dict[str, Any] = {"day": rng.choice(["2026-11-18", "2024-02-29"])}
    pools: dict[str, list[Any]] = {
        "at": ["09:30:00Z", "23:59:59.5+02:00", None],
        "wait": ["P1D", "PT0.5S", "-PT1M", "P999999999D"],
        "amount": ["12.50", 7, "-100.5", "999.99", "0"],
        "loose": ["123456.789", None, 10**20],
        "blob": ["AA==", "QUJD", "QUJDREVG"],
        "blobs": [["AA==", "QUI="], []],
        "stamps": [{"a": "2026-01-01", "b": "2026-12-31"}, {}],
    }
    for key, pool in pools.items():
        if rng.random() < 0.7:
            data[key] = rng.choice(pool)
    return data


def wire_outcome(call: Any, data: object) -> tuple[str, Any]:
    """Like ``outcome``, comparing accepted instances by ``repr`` so a decimal
    scale (``1.50`` against ``1.5``) or a time zone cannot differ unseen."""
    kind, value = outcome(call, Wire, data)
    return (kind, repr(value)) if kind == "ok" else (kind, value)


@pytest.mark.parametrize("seed", range(8))
def test_wire_scalar_inputs_agree(seed: int) -> None:
    rng = random.Random(900 + seed)
    for _ in range(600):
        data = mutate(rng, valid_wire(rng))
        if rng.random() < 0.3:
            data[rng.choice(list(Wire.specs()))] = rng.choice(WIRE_JUNK)
        assert wire_outcome(native, data) == wire_outcome(oracle, data), data


@pytest.mark.parametrize("junk", WIRE_JUNK)
def test_every_junk_value_in_every_wire_slot_agrees(junk: Any) -> None:
    base: dict[str, Any] = {
        "day": "2026-11-18", "at": "09:30:00Z", "wait": "P1D", "amount": "1.50",
        "loose": "1", "blob": "AA==", "blobs": ["AA=="], "stamps": {"a": "2026-01-01"},
    }  # fmt: skip
    for key in list(base):
        data = {**base, key: junk}
        assert wire_outcome(native, data) == wire_outcome(oracle, data), (key, junk)
    for key in ("blobs", "stamps"):
        inner = [junk] if key == "blobs" else {"a": junk}
        data = {**base, key: inner}
        assert wire_outcome(native, data) == wire_outcome(oracle, data), (key, junk)


class Shade(enum.StrEnum):
    DARK = "dark"
    LIGHT = "light"


class Rank(enum.IntEnum):
    LOW = 1
    HIGH = 3


class Mode(enum.Enum):
    FAST = "fast"
    SLOW = "slow"


class Choice(Schema):
    shade: Shade
    rank: Rank | None = None
    mode: Mode | Unset = UNSET
    kind: Literal["a", "b"] = "a"
    size: Literal[1, 2] | None = None
    shades: list[Shade] = Field(default_factory=list)
    ranks: dict[str, Rank] = Field(default_factory=dict)


ENUM_JUNK: list[Any] = [
    *JUNK, "dark", "light", "DARK", "fast", "slow", "a", "b", 1, 2, 3, 1.0,
    Shade.DARK, Rank.HIGH, Mode.FAST,
]  # fmt: skip


def enum_outcome(call: Any, data: object) -> tuple[str, Any]:
    kind, value = outcome(call, Choice, data)
    return (kind, repr(value)) if kind == "ok" else (kind, value)


def test_every_junk_value_in_every_enum_slot_agrees() -> None:
    base: dict[str, Any] = {
        "shade": "dark", "rank": 1, "mode": "fast", "kind": "a", "size": 1,
        "shades": ["dark"], "ranks": {"a": 3},
    }  # fmt: skip
    for junk in ENUM_JUNK:
        for key in list(base):
            data = {**base, key: junk}
            assert enum_outcome(native, data) == enum_outcome(oracle, data), (key, junk)
        for key, inner in (("shades", [junk]), ("ranks", {"a": junk})):
            data = {**base, key: inner}
            assert enum_outcome(native, data) == enum_outcome(oracle, data), (key, junk)
