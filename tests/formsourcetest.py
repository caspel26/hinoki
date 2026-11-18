"""``Form[Schema]``: a Schema read from a form, text converted before checking."""

import dataclasses
from typing import Any
from uuid import UUID

import pytest

from hinoki import Form, FormParser, Hinoki, JSONParser, Schema, http
from hinoki.errors import RegistrationError, ValidationError
from tests.harness import Client, Pool

FORM = "application/x-www-form-urlencoded"


class Signup(Schema):
    name: str
    age: int
    ratio: float = 1.0
    active: bool = False
    tags: list[int] = dataclasses.field(default_factory=list)
    token: UUID | None = None


@http.post("/signup", status=201)
def signup(data: Form[Signup]) -> dict[str, Any]:
    return {
        "name": data.name,
        "age": data.age,
        "ratio": data.ratio,
        "active": data.active,
        "tags": data.tags,
    }


def post(client: Client, body: bytes, content_type: str = FORM) -> Any:
    return client.request("POST", "/signup", body, content_type=content_type)


async def test_text_is_converted_to_the_declared_types(pool: Pool) -> None:
    client = await pool.start(Hinoki(include=[signup]))
    reply = await post(client, b"name=Ada&age=36&ratio=2.5&active=true&tags=1&tags=2")
    assert reply.status == 201
    assert reply.body == {
        "name": "Ada",
        "age": 36,
        "ratio": 2.5,
        "active": True,
        "tags": [1, 2],
    }


async def test_a_single_value_is_a_one_item_list(pool: Pool) -> None:
    client = await pool.start(Hinoki(include=[signup]))
    reply = await post(client, b"name=Ada&age=1&tags=7")
    assert reply.body["tags"] == [7]


@pytest.mark.parametrize(
    "body",
    [b"name=Ada&age=old", b"name=Ada&age=1&age=2", b"name=Ada&age=", b"age=1"],
)
async def test_what_does_not_convert_is_refused_by_the_strict_schema(
    pool: Pool, body: bytes
) -> None:
    client = await pool.start(Hinoki(include=[signup]))
    reply = await post(client, body)
    assert reply.status == 422
    assert reply.body["error"] == "validation_error"


async def test_json_stays_strict(pool: Pool) -> None:
    @http.post("/both", parsers=[JSONParser()])
    def both(data: Signup) -> dict[str, int]:
        return {"age": data.age}

    client = await pool.start(Hinoki(include=[both]))
    reply = await client.request(
        "POST", "/both", b'{"name": "a", "age": "3"}', content_type="application/json"
    )
    assert reply.status == 422


def test_validate_text_converts_then_validates() -> None:
    value = Signup.validate_text({"name": ["a"], "age": ["3"]})
    assert value.age == 3
    big = Signup.validate_text({"name": ["a"], "age": ["123456789012345678901"]})
    assert big.age == 123456789012345678901
    token = "12345678-1234-5678-1234-567812345678"  # noqa: S105
    full = Signup.validate_text(
        {"name": ["a"], "age": ["1"], "token": [token], "active": ["0"]}
    )
    assert (full.token, full.active) == (UUID(token), False)
    with pytest.raises(ValidationError):
        Signup.validate_text({"name": ["a"], "age": ["3.5"]})


def test_registration_rules() -> None:
    @http.get("/q")
    def query(data: Form[Signup]) -> None: ...

    with pytest.raises(RegistrationError, match="POST/PUT/PATCH/QUERY"):
        Hinoki(include=[query])

    @http.post("/j", parsers=[JSONParser()])
    def json_only(data: Form[Signup]) -> None: ...

    with pytest.raises(RegistrationError, match="only form parsers"):
        Hinoki(include=[json_only])

    @http.post("/m", parsers=[FormParser(), JSONParser()])
    def mixed(data: Form[Signup]) -> None: ...

    with pytest.raises(RegistrationError, match="only form parsers"):
        Hinoki(include=[mixed])

    @http.post("/n")
    def not_schema(data: Form[int]) -> None: ...

    with pytest.raises(RegistrationError, match="needs a Schema"):
        Hinoki(include=[not_schema])


def test_openapi_describes_only_form_media_types() -> None:
    content = Hinoki(include=[signup]).openapi()["paths"]["/signup"]["post"][
        "requestBody"
    ]["content"]
    assert FORM in content
    assert "application/json" not in content
