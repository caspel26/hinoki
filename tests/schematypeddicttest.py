"""``TypedDict`` and dataclass annotations as ``Schema`` fields: each becomes a
generated nested ``Schema`` with the same fields (validation, dump, OpenAPI, the
request lanes)."""

from __future__ import annotations

import dataclasses
import json
from typing import Annotated, Any, NotRequired, Required, TypedDict

import pytest
from openapi_spec_validator import validate

from hinoki import UNSET, Field, Hinoki, Schema, http
from hinoki.errors import RegistrationError, ValidationError
from hinoki.testing import ASGIClient


class Address(TypedDict):
    street: str
    zip: NotRequired[str]


class Loose(TypedDict, total=False):
    note: str
    count: Required[int]


@dataclasses.dataclass
class Geo:
    lat: float
    lon: float = 0.0
    tags: list[str] = dataclasses.field(default_factory=list)


class Place(Schema):
    name: str
    address: Address
    geo: Geo | None = None
    extra: Loose | None = None
    others: list[Geo] = Field(default_factory=list)


GOOD = {"name": "home", "address": {"street": "1 Main"}}
BAD = b'{"name": "x", "address": {}}'
HEADERS = {"content-type": "application/json"}


def details(data: dict[str, object]) -> list[tuple[str, str]]:
    with pytest.raises(ValidationError) as caught:
        Place.validate(data)
    return [(d.path, d.code) for d in caught.value.details]


def test_a_nested_object_is_validated_and_read_by_attribute() -> None:
    place = Place.validate(
        {
            **GOOD,
            "geo": {"lat": 1.5},
            "others": [{"lat": 2, "lon": 3, "tags": ["a"]}],
        }
    )
    address: Any = place.address
    assert address.street == "1 Main"
    assert address.zip is UNSET
    assert place.geo is not None
    assert (place.geo.lat, place.geo.lon, place.geo.tags) == (1.5, 0.0, [])
    assert place.others[0].tags == ["a"]
    assert type(place.geo).__name__ == "Geo"


def test_dump_keeps_only_the_given_keys_of_a_typeddict() -> None:
    place = Place.validate({**GOOD, "extra": {"count": 3}})
    assert place.dump()["address"] == {"street": "1 Main"}
    assert place.dump()["extra"] == {"count": 3}


def test_errors_carry_the_nested_path() -> None:
    assert details({"name": "x", "address": {}}) == [("address.street", "required")]
    assert details({**GOOD, "geo": {"lat": "north"}}) == [("geo.lat", "type")]
    assert details({**GOOD, "extra": {"note": "n"}}) == [("extra.count", "required")]


def test_the_same_class_is_one_schema() -> None:
    first = Place.specs()["geo"].ftype.schema
    again = Place.specs()["others"].ftype.item
    assert again is not None and first is again.schema


@dataclasses.dataclass
class Hidden:
    a: int = dataclasses.field(default=0, init=False)


@dataclasses.dataclass
class Node:
    child: Node | None = None


def test_a_non_init_or_recursive_dataclass_is_refused() -> None:
    for bad in (Hidden, Node):
        with pytest.raises(RegistrationError):
            type("Bad", (Schema,), {"__annotations__": {"v": bad}})


def test_constraints_on_the_field_itself_are_refused() -> None:
    with pytest.raises(RegistrationError):
        type(
            "Bad",
            (Schema,),
            {"__annotations__": {"v": Annotated[Geo, Field(min_length=1)]}},
        )


@http.post("/typed/place")
async def echo_place(data: Place) -> Place:
    return data


def test_openapi_has_a_component_per_type() -> None:
    app = Hinoki()
    app.include(echo_place)
    document = app.openapi()
    validate(document)
    schemas = document["components"]["schemas"]
    assert schemas["Address"]["required"] == ["street"]
    assert set(schemas["Geo"]["properties"]) == {"lat", "lon", "tags"}
    assert schemas["Geo"]["required"] == ["lat"]


async def test_the_request_lanes_validate_the_nested_object() -> None:
    app = Hinoki()
    app.include(echo_place)
    async with ASGIClient(app) as client:
        good = await client.post(
            "/typed/place", body=json.dumps(GOOD).encode(), headers=HEADERS
        )
        bad = await client.post("/typed/place", body=BAD, headers=HEADERS)
    assert good.status == 200
    assert good.body["address"] == {"street": "1 Main"}
    assert bad.status == 422


class Marked(TypedDict):
    code: Annotated[str, Field(min_length=2)]
    note: NotRequired[Annotated[str, Field(max_length=5)]]


def test_an_annotated_typeddict_key_keeps_its_requirement_and_constraints() -> None:
    class Holder(Schema):
        marked: Marked

    marked: Any = Holder.validate({"marked": {"code": "ab"}}).marked
    assert marked.code == "ab"
    with pytest.raises(ValidationError):
        Holder.validate({"marked": {"code": "a"}})
    with pytest.raises(ValidationError):
        Holder.validate({"marked": {}})
