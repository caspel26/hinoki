"""The Rust validator must agree with the pure-Python oracle on every input."""

import math
import random
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

import pytest

from hinoki import UNSET, Field, Schema, Unset
from hinoki.errors import ErrorDetail, ValidationError
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


def native(cls: type[Schema], data: object) -> Any:
    return cls.validate(data)


def oracle(cls: type[Schema], data: object) -> Any:
    """The old ``Schema.validate``, kept as the reference implementation."""
    validator = Validator()
    try:
        from hinoki.fieldtypes import FieldType

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
