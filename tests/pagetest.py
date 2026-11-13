"""Pagination wired through decorators, binding, responses and OpenAPI."""

import pytest
from openapi_spec_validator import validate

from hinoki import HinokiAPI, RegistrationError, Schema, get
from hinoki.pagination import Page, PageNumberPagination, PageParams
from tests.harness import Pool

strategy = PageNumberPagination(page_size=2, max_page_size=5)
ROWS = [f"item-{n}" for n in range(5)]


class ItemOut(Schema):
    name: str


@get("/items", pagination=strategy)
async def items(page: PageParams) -> Page[ItemOut]:
    return Page.of(ROWS, page).map(lambda name: ItemOut(name=name))


@get("/orphan")
async def orphan(page: PageParams) -> Page[ItemOut]:
    raise NotImplementedError


@get("/unused", pagination=strategy)
async def unused() -> None: ...


def make_app() -> HinokiAPI:
    app = HinokiAPI()
    app.include(items)
    return app


async def test_default_page_and_totals(pool: Pool) -> None:
    client = await pool.start(make_app())
    reply = await client.request("GET", "/items")
    assert reply.body == {
        "items": [{"name": "item-0"}, {"name": "item-1"}],
        "page": 1,
        "page_size": 2,
        "total": 5,
        "pages": 3,
    }


async def test_explicit_page_and_size(pool: Pool) -> None:
    client = await pool.start(make_app())
    reply = await client.request("GET", "/items", query="page=3&page_size=2")
    assert reply.body["items"] == [{"name": "item-4"}]
    beyond = await client.request("GET", "/items", query="page=9")
    assert beyond.body["items"] == []


@pytest.mark.parametrize(
    ("query", "path", "code"),
    [
        ("page=0", "page", "type"),
        ("page_size=6", "page_size", "le"),
        ("page=x", "page", "type"),
    ],
)
async def test_invalid_page_parameters_give_422(
    pool: Pool, query: str, path: str, code: str
) -> None:
    client = await pool.start(make_app())
    reply = await client.request("GET", "/items", query=query)
    assert reply.status == 422
    assert {"path": path, "code": code} in [
        {"path": d["path"], "code": d["code"]} for d in reply.body["details"]
    ]


@pytest.mark.parametrize("handler", [orphan, unused])
def test_pagination_option_and_parameter_must_come_together(handler: object) -> None:
    with pytest.raises(RegistrationError):
        HinokiAPI().include(handler)


def test_openapi_documents_the_query_parameters_and_the_page_schema() -> None:
    document = make_app().openapi()
    operation = document["paths"]["/items"]["get"]
    names = {p["name"]: p for p in operation["parameters"]}
    assert names["page_size"]["schema"]["maximum"] == 5
    assert names["page_size"]["schema"]["default"] == 2
    response = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert response == {"$ref": "#/components/schemas/Page_ItemOut"}
    page = document["components"]["schemas"]["Page_ItemOut"]
    assert page["properties"]["items"]["items"] == {
        "$ref": "#/components/schemas/ItemOut"
    }
    validate(document)
