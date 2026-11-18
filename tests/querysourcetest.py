"""``Query[Schema]``: a Schema read from the whole query string."""

import dataclasses
from typing import Any

import pytest
from openapi_spec_validator import validate

from hinoki import Hinoki, Query, Schema, http
from hinoki.errors import RegistrationError
from tests.harness import Pool


class Filters(Schema):
    term: str
    limit: int = 10
    active: bool = False
    ids: list[int] = dataclasses.field(default_factory=list)


@http.get("/items")
def items(filters: Query[Filters]) -> dict[str, Any]:
    return {
        "term": filters.term,
        "limit": filters.limit,
        "active": filters.active,
        "ids": filters.ids,
    }


@http.delete("/items/{item_id}")
def remove(item_id: int, filters: Query[Filters]) -> dict[str, Any]:
    return {"id": item_id, "limit": filters.limit}


async def test_query_text_is_converted_to_the_declared_types(pool: Pool) -> None:
    client = await pool.start(Hinoki(include=[items, remove]))
    reply = await client.request(
        "GET", "/items", query="term=a&limit=5&active=1&ids=1&ids=2"
    )
    assert reply.body == {"term": "a", "limit": 5, "active": True, "ids": [1, 2]}
    one = await client.request("GET", "/items", query="term=a&ids=7")
    assert one.body["ids"] == [7]
    deleted = await client.request("DELETE", "/items/3", query="term=x&limit=2")
    assert deleted.body == {"id": 3, "limit": 2}


@pytest.mark.parametrize(
    "target",
    ["", "term=a&limit=x", "term=a&limit=1&limit=2", "term=a&zzz=1"],
)
async def test_what_does_not_convert_is_a_422(pool: Pool, target: str) -> None:
    client = await pool.start(Hinoki(include=[items]))
    reply = await client.request("GET", "/items", query=target)
    assert reply.status == 422
    assert reply.body["error"] == "validation_error"


def test_registration_rules() -> None:
    @http.get("/a")
    def mixed(filters: Query[Filters], extra: int = 1) -> None: ...

    with pytest.raises(RegistrationError, match="whole query string"):
        Hinoki(include=[mixed])

    @http.get("/b/{filters}")
    def in_path(filters: Query[Filters]) -> None: ...

    with pytest.raises(RegistrationError, match="path parameter"):
        Hinoki(include=[in_path])

    @http.get("/c")
    def twice(a: Query[Filters], b: Query[Filters]) -> None: ...

    with pytest.raises(RegistrationError, match="at most one"):
        Hinoki(include=[twice])


def test_openapi_lists_one_query_parameter_per_field() -> None:
    document = Hinoki(include=[items]).openapi()
    validate(document)
    parameters = document["paths"]["/items"]["get"]["parameters"]
    assert {p["name"]: p["required"] for p in parameters} == {
        "term": True,
        "limit": False,
        "active": False,
        "ids": False,
    }
    assert all(p["in"] == "query" for p in parameters)
