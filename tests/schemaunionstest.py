"""Plain unions (``int | str``) and ``Any`` / ``object`` Schema fields: variant
order, refusals with their code and path, construction, output and OpenAPI."""

from __future__ import annotations

import datetime as dt
import time
from typing import Annotated, Any

import pytest
from openapi_spec_validator import validate

from hinoki import UNSET, Field, Hinoki, Schema, Unset, http
from hinoki.errors import RegistrationError, ValidationError
from hinoki.orm import Model, field
from hinoki.rules import Project, RuleSet, Schemas
from hinoki.schema import encode
from hinoki.testing import ASGIClient


class Point(Schema):
    x: int


class Mixed(Schema):
    code: int | str
    when: dt.date | int
    many: list[int | str]
    either: Point | int
    maybe: str | int | None = None
    anything: Any = None
    thing: object = None


def valid(**changes: object) -> dict[str, object]:
    data: dict[str, object] = {
        "code": "abc",
        "when": "2026-01-02",
        "many": [1, "a"],
        "either": {"x": 1},
        **changes,
    }
    return data


def problems(cls: type[Schema], data: object) -> list[tuple[str, str, str]]:
    with pytest.raises(ValidationError) as caught:
        cls.validate(data)
    return [(d.path, d.code, d.message) for d in caught.value.details]


def test_the_first_matching_variant_wins() -> None:
    item = Mixed.validate(valid(code=7, many=[1, "2", 3]))
    assert item.code == 7 and type(item.code) is int
    assert item.when == dt.date(2026, 1, 2)
    assert item.many == [1, "2", 3]
    assert item.either == Point(x=1)
    assert Mixed.validate(valid(when=5)).when == 5
    assert Mixed.validate(valid(either=4)).either == 4


def test_variants_are_tried_in_declared_order() -> None:
    class Loose(Schema):
        number: float | int
        exact: int | float

    item = Loose.validate({"number": 1, "exact": 1})
    assert type(item.number) is float and type(item.exact) is int


def test_booleans_are_not_integers() -> None:
    assert problems(Mixed, valid(code=True)) == [
        ("code", "union", "does not match any allowed type")
    ]


@pytest.mark.parametrize(
    ("changes", "path"),
    [
        ({"code": 1.5}, "code"),
        ({"when": "nope"}, "when"),
        ({"many": [1, 2.5]}, "many[1]"),
        ({"either": {"x": "no"}}, "either"),
        ({"either": []}, "either"),
    ],
)
def test_no_matching_variant_is_one_union_error(
    changes: dict[str, object], path: str
) -> None:
    found = problems(Mixed, valid(**changes))
    assert [(p, c) for p, c, _ in found] == [(path, "union")]


def test_null_is_refused_without_a_nullable_member() -> None:
    assert [c for _, c, _ in problems(Mixed, valid(code=None))] == ["null"]
    assert problems(Mixed, valid(many=[1, None]))[0][:2] == ("many[1]", "null")


def test_trial_errors_do_not_leak_into_the_result() -> None:
    found = problems(Mixed, valid(code=[], when="x"))
    assert [(p, c) for p, c, _ in found] == [("code", "union"), ("when", "union")]


def test_nullable_and_unset_and_defaults() -> None:
    assert Mixed.validate(valid(maybe=None)).maybe is None
    assert Mixed.validate(valid(maybe=3)).maybe == 3
    assert Mixed.validate(valid()).maybe is None

    class Optional(Schema):
        value: int | str | Unset = UNSET

    assert Optional.validate({}).value is UNSET
    assert Optional.validate({"value": "x"}).value == "x"


def test_a_union_of_schemas_still_needs_a_discriminator() -> None:
    other = type("Other", (Schema,), {"__annotations__": {"y": int}})
    with pytest.raises(RegistrationError, match="discriminator"):
        type("Bad", (Schema,), {"__annotations__": {"pet": Point | other}})


def test_any_accepts_every_value_unchanged() -> None:
    payload = {"a": [1, 2.5, None, {"b": True}], "c": "d"}
    item = Mixed.validate(valid(anything=payload, thing=[1, "x"]))
    assert item.anything == payload and item.thing == [1, "x"]
    assert Mixed.validate(valid(anything=None)).anything is None
    assert Mixed.validate(valid(anything=0)).anything == 0


class Bag(Schema):
    meta: dict[str, Any]
    rows: list[Any]
    pair: tuple[Any, int]


def test_any_works_inside_containers() -> None:
    bag = Bag.validate({"meta": {"k": [1]}, "rows": [1, "a", None], "pair": [{}, 2]})
    assert bag.meta == {"k": [1]} and bag.rows == [1, "a", None]
    assert problems(Bag, {"meta": {}, "rows": [], "pair": [1]}) == [
        ("pair", "min_length", "at least 2 items")
    ]


def test_construction_checks_the_variants() -> None:
    item = Mixed(
        code=3,
        when=dt.date(2026, 1, 2),
        many=[1, "a"],
        either=Point(x=2),
        anything=object(),
    )
    assert item.code == 3 and item.either == Point(x=2)
    with pytest.raises(ValidationError):
        Mixed(code=1.5, when=1, many=[], either=1)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Mixed(code=1, when="2026-01-02", many=[], either=1)  # type: ignore[arg-type]


def test_construction_does_not_turn_a_date_into_text() -> None:
    class Stamp(Schema):
        value: str | dt.date

    day = dt.date(2026, 1, 2)
    assert Stamp(value=day).value == day
    assert Stamp(value="2026").value == "2026"


def test_output_writes_the_value_that_was_accepted() -> None:
    item = Mixed.validate(valid(anything={"a": [1]}))
    assert encode(item) == {
        "code": "abc",
        "when": "2026-01-02",
        "many": [1, "a"],
        "either": {"x": 1},
        "maybe": None,
        "anything": {"a": [1]},
        "thing": None,
    }
    assert item.to_dict()["when"] == dt.date(2026, 1, 2)


@pytest.mark.parametrize(
    "annotation",
    [
        Annotated[int | str, Field(ge=1)],
        Annotated[Any, Field(min_length=1)],
        Annotated[object, Field(pattern="a")],
    ],
)
def test_constraints_that_do_not_apply_are_refused(annotation: Any) -> None:
    with pytest.raises(RegistrationError):
        type("Bad", (Schema,), {"__annotations__": {"value": annotation}})


class Cat(Schema):
    kind: Annotated[str, Field(one_of=["cat"])]


class Dog(Schema):
    kind: Annotated[str, Field(one_of=["dog"])]


class Owner(Schema):
    pet: Annotated[Cat | Dog, Field(discriminator="kind")] | int


def test_a_discriminated_union_can_be_a_variant() -> None:
    assert Owner.validate({"pet": {"kind": "dog"}}).pet == Dog(kind="dog")
    assert Owner.validate({"pet": 3}).pet == 3


def test_too_many_variants_are_refused() -> None:
    many = (
        int
        | str
        | float
        | bool
        | bytes
        | dt.date
        | dt.time
        | dt.timedelta
        | list[int]
        | list[str]
        | list[float]
        | list[bool]
        | dict[str, int]
        | dict[str, str]
        | set[int]
        | tuple[int, ...]
        | frozenset[int]
    )
    with pytest.raises(RegistrationError, match="union"):
        type("Bad", (Schema,), {"__annotations__": {"value": many}})


class Frozen(Schema):
    ids: frozenset[int | str]


def test_union_variants_that_cannot_be_hashed_are_not_set_members() -> None:
    assert Frozen.validate({"ids": [1, "a", 1]}).ids == frozenset({1, "a"})
    with pytest.raises(RegistrationError, match="hashable"):
        type("Bad", (Schema,), {"__annotations__": {"ids": set[int | list[int]]}})


def test_a_dict_column_is_json() -> None:
    class Entry(Model):
        id: int = field(primary_key=True)
        data: dict[str, Any]

    assert "data" in Entry.__json_columns__


@pytest.mark.parametrize(
    ("column", "reason"),
    [
        (int | str, "a union of several types"),
        (dt.date | int | None, "a union of several types"),
        (Any, "no column type holds Any"),
    ],
)
def test_a_union_or_any_column_is_refused(column: object, reason: str) -> None:
    with pytest.raises(RegistrationError, match=rf"Entry\.tag: {reason}"):
        type(
            "Entry",
            (Model,),
            {
                "__annotations__": {"id": int, "tag": column},
                "id": field(primary_key=True),
            },
        )


@http.post("/unions/echo")
def echo(data: Mixed) -> Mixed:
    return data


def test_openapi_documents_unions_and_any() -> None:
    app = Hinoki()
    app.include(echo)
    document = app.openapi()
    validate(document)
    properties = document["components"]["schemas"]["Mixed"]["properties"]
    assert properties["code"] == {"anyOf": [{"type": "integer"}, {"type": "string"}]}
    assert properties["many"]["items"] == {
        "anyOf": [{"type": "integer"}, {"type": "string"}]
    }
    assert properties["maybe"]["anyOf"][-1] == {"type": "null"}
    assert properties["anything"] == {}
    assert properties["thing"] == {}


async def test_request_bodies_and_responses_use_unions() -> None:
    app = Hinoki()
    app.include(echo)
    async with ASGIClient(app) as client:
        good = await client.post("/unions/echo", json=valid(anything={"k": [1]}))
        assert good.status == 200, good.body
        assert good.body["code"] == "abc" and good.body["anything"] == {"k": [1]}
        bad = await client.post("/unions/echo", json=valid(code=1.5))
        assert bad.status == 422
        assert [(d["path"], d["code"]) for d in bad.body["details"]] == [
            ("data.code", "union")
        ]


class Loose(Schema):
    text: int | str


class Bounded(Schema):
    text: int | Annotated[str, Field(max_length=5)]


@http.post("/unions/loose")
def loose(data: Loose) -> dict[str, int]:
    return {}


@http.post("/unions/bounded")
def bounded(data: Bounded) -> dict[str, int]:
    return {}


def test_string_limits_rule_looks_inside_unions() -> None:
    app = Hinoki()
    app.include(loose, bounded)
    found = RuleSet(Schemas.require_string_limits()).run(Project(app))
    assert [d.subject.rsplit(".", 1)[-1] for d in found] == ["text"]
    assert len(found) == 1 and "Loose" in found[0].subject


class Branch(Schema):
    kids: list[Branch] | tuple[Branch, ...] | None = None
    leaf: int = 0


def test_nested_unions_with_a_late_failure_stay_bounded() -> None:
    levels = 40
    value: dict[str, Any] = {"leaf": "x"}
    for _ in range(levels):
        value = {"kids": [value], "leaf": 0}
    started = time.monotonic()
    with pytest.raises(ValidationError):
        Branch.validate(value)
    assert time.monotonic() - started < 2
    good: dict[str, Any] = {"leaf": 1}
    for _ in range(levels):
        good = {"kids": [good], "leaf": 0}
    assert Branch.validate(good).kids is not None
