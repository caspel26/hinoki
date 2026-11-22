"""Valid and invalid request bodies generated from the OpenAPI document."""

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Literal

import pytest

from hinoki import Hinoki, Schema, http
from hinoki.ai.samples import CannotSample, invalid_values, request_cases, sample_value
from hinoki.schema import Field
from hinoki.testing import ASGIClient


class Address(Schema):
    city: Annotated[str, Field(min_length=2)]


class Item(Schema):
    name: Annotated[str, Field(min_length=2, max_length=5)]
    qty: Annotated[int, Field(ge=1, le=9)]
    ratio: Annotated[float, Field(gt=0, lt=1)]
    price: Decimal
    kind: Literal["a", "b"]
    when: dt.datetime
    day: dt.date
    uid: uuid.UUID
    flag: bool
    address: Address
    tags: Annotated[list[str], Field(min_length=1)] = Field(
        default_factory=lambda: ["x"]
    )
    scores: dict[str, int] = Field(default_factory=dict)
    note: str | None = None


class Coded(Schema):
    code: Annotated[str, Field(pattern=r"^[A-Z]{3}-\d{4}$")]


@http.post("/items")
async def make(body: Item) -> Item:
    return body


@http.post("/coded")
async def code(body: Coded) -> Coded:
    return body


@http.get("/plain")
async def plain() -> dict[str, int]:
    return {}


def test_a_sample_satisfies_every_constraint_of_its_schema() -> None:
    document = Hinoki(include=[make]).openapi()
    value = sample_value({"$ref": "#/components/schemas/Item"}, document)
    parsed = Item.validate(value)
    assert 2 <= len(parsed.name) <= 5
    assert 1 <= parsed.qty <= 9
    assert 0 < parsed.ratio < 1
    assert parsed.kind in {"a", "b"}
    assert parsed.address.city


def test_samples_are_deterministic() -> None:
    document = Hinoki(include=[make]).openapi()
    ref = {"$ref": "#/components/schemas/Item"}
    assert sample_value(ref, document) == sample_value(ref, document)


def test_every_invalid_value_breaks_exactly_one_rule() -> None:
    document = Hinoki(include=[make]).openapi()
    ref = {"$ref": "#/components/schemas/Item"}
    broken = dict(invalid_values(ref, document))
    for label in (
        "missing name",
        "name has the wrong type",
        "name is too short",
        "name is too long",
        "qty is below the minimum",
        "qty is above the maximum",
        "kind is not an allowed value",
        "address.city is too short",
        "missing address.city",
        "unexpected property",
    ):
        assert label in broken, label


def test_a_pattern_without_a_known_format_cannot_be_sampled() -> None:
    document = Hinoki(include=[code]).openapi()
    with pytest.raises(CannotSample, match="pattern"):
        sample_value({"$ref": "#/components/schemas/Coded"}, document)


def test_request_cases_cover_only_operations_with_a_body() -> None:
    cases = request_cases(Hinoki(include=[make, plain]).openapi())
    assert [(c.method, c.path) for c in cases] == [("POST", "/items")]
    assert cases[0].invalid


async def test_the_app_accepts_every_valid_body_and_rejects_every_invalid_one() -> None:
    app = Hinoki(include=[make])
    async with ASGIClient(app) as client:
        for case in request_cases(app.openapi()):
            good = await client.post(case.path, json=case.body)
            assert good.status == 200, good.body
            for label, body in case.invalid:
                bad = await client.post(case.path, json=body)
                assert bad.status == 422, (label, bad.status)


def test_a_sample_follows_nullable_types_constants_and_untyped_objects() -> None:
    assert sample_value({"type": ["null", "string"]}, {}) == "a"
    assert sample_value({"const": 7}, {}) == 7
    untyped = {"properties": {"name": {"type": "string"}}, "required": ["name"]}
    assert sample_value(untyped, {}) == {"name": "a"}


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        ({"$ref": "#/components/schemas/Missing"}, "unresolved reference"),
        ({"description": "no type"}, "no sample for schema"),
        (
            {"type": "integer", "minimum": 1, "maximum": 1, "multipleOf": 2},
            "no value to sample",
        ),
        (
            {"type": "array", "minItems": 2, "uniqueItems": True},
            "several unique items",
        ),
    ],
)
def test_a_schema_that_cannot_be_sampled_is_reported(
    schema: dict[str, object], message: str
) -> None:
    with pytest.raises(CannotSample, match=message):
        sample_value(schema, {"components": {"schemas": {}}})


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"type": "integer", "exclusiveMinimum": 3}, 4),
        ({"type": "integer", "maximum": -5}, -6),
        ({"type": "number", "maximum": 2.5}, 2.5),
        ({"type": "array"}, []),
        (
            {"type": "array", "prefixItems": [{"type": "string"}, {"type": "integer"}]},
            ["a", 1],
        ),
    ],
)
def test_samples_respect_open_and_upper_only_bounds_and_positional_items(
    schema: dict[str, object], expected: object
) -> None:
    assert sample_value(schema, {}) == expected


def test_a_recursive_schema_is_cut_when_optional_and_refused_when_required() -> None:
    def node(required: list[str]) -> dict[str, object]:
        return {
            "type": "object",
            "required": required,
            "properties": {"child": {"$ref": "#/components/schemas/Node"}},
        }

    def document(required: list[str]) -> dict[str, object]:
        return {"components": {"schemas": {"Node": node(required)}}}

    optional = document([])
    sample = sample_value(node([]), optional)
    depth = 0
    while "child" in sample:
        sample = sample["child"]
        depth += 1
    assert 0 < depth < 10
    with pytest.raises(CannotSample, match="recursive schema"):
        sample_value(node(["child"]), document(["child"]))


def test_a_list_with_a_maximum_is_invalid_one_item_past_it() -> None:
    schema = {
        "type": "object",
        "required": ["tags"],
        "properties": {
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2}
        },
    }
    cases = dict(invalid_values(schema, {}))
    assert cases["tags has too many items"] == {"tags": ["a", "a", "a"]}
