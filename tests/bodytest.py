"""Request bodies, query parameters and schema-based responses."""

import json
from typing import Annotated
from uuid import UUID

import pytest

from hinoki import HinokiAPI, RegistrationError, Schema, get, post
from hinoki.request import Request
from hinoki.runtime.requests import MAX_BODY
from hinoki.schema import Field
from tests.harness import Client, Pool

JSON = "application/json"


class PostCreate(Schema):
    title: Annotated[str, Field(min_length=1, max_length=10)]
    tags: list[str] = Field(default_factory=list)


class PostOut(Schema):
    id: UUID
    title: str
    tags: list[str]


class Other(Schema):
    name: str


POST_ID = UUID(int=9)


@post("/posts", status=201)
async def create(request: Request, data: PostCreate) -> PostOut:
    return PostOut(id=POST_ID, title=data.title, tags=data.tags)


@get("/posts")
def search(
    request: Request, q: str, limit: int = 10, active: bool = False
) -> list[PostOut]:
    return [PostOut(id=POST_ID, title=q, tags=[str(limit), str(active)])]


@get("/mixed/{kind}")
async def mixed(request: Request, kind: str, page: int = 1) -> dict[str, object]:
    return {"kind": kind, "page": page}


@post("/two")
def two_bodies(request: Request, a: PostCreate, b: Other) -> None: ...


@get("/getbody")
def get_with_body(request: Request, data: PostCreate) -> None: ...


@post("/items/{item}")
def schema_in_path(request: Request, item: PostCreate) -> None: ...


def make_app() -> HinokiAPI:
    app = HinokiAPI()
    app.include(create, search, mixed)
    return app


async def start(pool: Pool) -> Client:
    return await pool.start(make_app())


def payload(**fields: object) -> bytes:
    return json.dumps(fields).encode()


async def test_valid_body_is_validated_and_the_response_serialised(pool: Pool) -> None:
    client = await start(pool)
    reply = await client.request(
        "POST", "/posts", payload(title="Hi", tags=["a"]), content_type=JSON
    )
    assert reply.status == 201
    assert reply.body == {"id": str(POST_ID), "title": "Hi", "tags": ["a"]}


async def test_validation_errors_are_all_reported_with_body_paths(pool: Pool) -> None:
    client = await start(pool)
    reply = await client.request(
        "POST", "/posts", payload(title="", tags=[1], extra=0), content_type=JSON
    )
    assert reply.status == 422
    found = {d["path"]: d["code"] for d in reply.body["details"]}
    assert found == {
        "data.title": "min_length",
        "data.tags[0]": "type",
        "data.extra": "extra_forbidden",
    }


@pytest.mark.parametrize(
    ("body", "content_type", "status", "code"),
    [
        (b"{not json", JSON, 400, "invalid_json"),
        (b"", JSON, 400, "invalid_json"),
        (b'{"title": NaN}', JSON, 400, "invalid_json"),
        (b'{"title": "x"}', "text/plain", 415, "unsupported_media_type"),
        (b'{"title": "x"}', None, 415, "unsupported_media_type"),
    ],
)
async def test_unusable_bodies_are_rejected_before_validation(
    pool: Pool, body: bytes, content_type: str | None, status: int, code: str
) -> None:
    client = await start(pool)
    reply = await client.request("POST", "/posts", body, content_type=content_type)
    assert (reply.status, reply.body["error"]) == (status, code)


async def test_content_type_parameters_are_accepted(pool: Pool) -> None:
    client = await start(pool)
    reply = await client.request(
        "POST",
        "/posts",
        payload(title="x"),
        content_type="application/json; charset=utf-8",
    )
    assert reply.status == 201


async def test_body_must_be_an_object(pool: Pool) -> None:
    client = await start(pool)
    reply = await client.request("POST", "/posts", b"[1]", content_type=JSON)
    assert reply.body["details"] == [
        {"path": "data", "code": "type", "message": "expected an object"}
    ]


async def test_query_parameters_are_converted_with_defaults(pool: Pool) -> None:
    client = await start(pool)
    reply = await client.request("GET", "/posts", query="q=hello&limit=3&active=true")
    assert reply.body[0]["title"] == "hello"
    assert reply.body[0]["tags"] == ["3", "True"]
    default = await client.request("GET", "/posts", query="q=x")
    assert default.body[0]["tags"] == ["10", "False"]


@pytest.mark.parametrize(
    ("query", "path", "code"),
    [
        ("", "q", "required"),
        ("q=a&limit=ten", "limit", "type"),
        ("q=a&q=b", "q", "duplicate"),
        ("q=a&active=yes", "active", "type"),
    ],
)
async def test_bad_query_parameters_give_422(
    pool: Pool, query: str, path: str, code: str
) -> None:
    client = await start(pool)
    reply = await client.request("GET", "/posts", query=query)
    assert reply.status == 422
    assert {"path": path, "code": code} in [
        {"path": d["path"], "code": d["code"]} for d in reply.body["details"]
    ]


async def test_path_and_query_parameters_combine(pool: Pool) -> None:
    client = await start(pool)
    reply = await client.request("GET", "/mixed/news", query="page=2")
    assert reply.body == {"kind": "news", "page": 2}


async def test_malformed_query_string_is_a_400(pool: Pool) -> None:
    client = await start(pool)
    reply = await client.request("GET", "/mixed/news", query="page=%FF")
    assert (reply.status, reply.body["error"]) == (400, "invalid_query")


@pytest.mark.parametrize("handler", [two_bodies, get_with_body, schema_in_path])
def test_ambiguous_schema_parameters_fail_at_include(handler: object) -> None:
    with pytest.raises(RegistrationError):
        HinokiAPI().include(handler)


async def test_oversized_body_is_rejected(pool: Pool) -> None:
    client = await start(pool)
    big = b"x" * (MAX_BODY + 1)
    reply = await client.request("POST", "/posts", big, content_type=JSON)
    assert reply.status == 413


async def test_the_body_is_not_read_for_actions_that_take_none(pool: Pool) -> None:
    client = await start(pool)
    big = b"x" * (MAX_BODY + 1)
    reply = await client.request("GET", "/mixed/news", big)
    assert reply.status == 200


async def test_deeply_nested_json_is_a_400_not_a_crash(pool: Pool) -> None:
    client = await start(pool)
    deep = b"[" * 100_000 + b"]" * 100_000
    reply = await client.request("POST", "/posts", deep, content_type=JSON)
    assert (reply.status, reply.body["error"]) == (400, "invalid_json")


async def test_non_ascii_raw_path_is_a_400(pool: Pool) -> None:
    client = await start(pool)
    reply = await client.request("GET", "/x", raw_path="/mixed/é".encode())
    assert (reply.status, reply.body["error"]) == (400, "bad_path_encoding")
