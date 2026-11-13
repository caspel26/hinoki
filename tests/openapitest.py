"""OpenAPI generation from declarations, validated against the 3.1 meta-schema."""

from typing import Annotated, Any
from uuid import UUID

import pytest
from openapi_spec_validator import validate

from hinoki import (
    API,
    Access,
    HinokiAPI,
    Principal,
    RegistrationError,
    Schema,
    get,
    post,
)
from hinoki.request import Request
from hinoki.schema import Field
from tests.harness import Pool
from tests.providers import TokenAuth


class AuthorOut(Schema):
    """Public author data."""

    id: UUID
    name: str


class PostCreate(Schema):
    """Input for creating a post."""

    title: Annotated[str, Field(min_length=1, max_length=160)]
    stars: Annotated[int, Field(ge=0, le=5)] = 0
    tags: Annotated[list[str], Field(max_length=3)] = Field(default_factory=list)
    subtitle: str | None = None


class PostOut(Schema):
    id: UUID
    title: str
    author: AuthorOut


class Tree(Schema):
    name: str
    children: list["Tree"] = Field(default_factory=list)


editor = Access(auth=TokenAuth(), scopes=["posts:write"])


@get("/health")
def health(request: Request) -> dict[str, str]:
    """Liveness probe.

    Always answers while the worker runs.
    """
    return {"status": "ok"}


class Posts(API, path="/posts"):
    @get("/")
    async def search(self, request: Request, q: str, limit: int = 10) -> list[PostOut]:
        return []

    @get("/{post_id}")
    def retrieve(self, request: Request, post_id: UUID) -> PostOut:
        raise NotImplementedError

    @post("/", status=201, access=editor)
    async def create(self, request: Request, data: PostCreate) -> PostOut:
        raise NotImplementedError

    @post("/{post_id}/publish")
    async def publish(self, request: Request, post_id: UUID) -> None: ...


@get("/tree")
def tree(request: Request) -> Tree:
    raise NotImplementedError


@get("/stats")
def stats(request: Request) -> dict[str, int]:
    return {}


def make_app(**options: Any) -> HinokiAPI:
    app = HinokiAPI(title="Blog", version="1.2.3", **options)
    app.include(health, Posts, tree, stats)
    return app


def test_document_is_valid_openapi_31() -> None:
    document = make_app().openapi()
    assert document["openapi"] == "3.1.0"
    assert document["info"] == {"title": "Blog", "version": "1.2.3"}
    validate(document)


def test_paths_methods_and_operation_ids_come_from_declarations() -> None:
    paths = make_app().openapi()["paths"]
    assert set(paths) == {
        "/health",
        "/posts",
        "/posts/{post_id}",
        "/posts/{post_id}/publish",
        "/tree",
        "/stats",
    }
    assert set(paths["/posts"]) == {"get", "post"}
    ids = [op["operationId"] for item in paths.values() for op in item.values()]
    assert len(ids) == len(set(ids))


def test_docstring_becomes_summary_and_description() -> None:
    health_op = make_app().openapi()["paths"]["/health"]["get"]
    assert health_op["summary"] == "Liveness probe."
    assert health_op["description"] == "Always answers while the worker runs."


def test_path_and_query_parameters() -> None:
    paths = make_app().openapi()["paths"]
    search = {p["name"]: p for p in paths["/posts"]["get"]["parameters"]}
    assert search["q"]["required"]
    assert search["q"]["in"] == "query"
    assert not search["limit"]["required"]
    assert search["limit"]["schema"] == {"type": "integer", "default": 10}
    retrieve = paths["/posts/{post_id}"]["get"]["parameters"][0]
    assert retrieve["in"] == "path"
    assert retrieve["schema"] == {"type": "string", "format": "uuid"}


def test_request_body_and_responses() -> None:
    create = make_app().openapi()["paths"]["/posts"]["post"]
    body = create["requestBody"]["content"]["application/json"]["schema"]
    assert body == {"$ref": "#/components/schemas/PostCreate"}
    assert set(create["responses"]) == {"201", "422", "401", "403"}
    ok = create["responses"]["201"]["content"]["application/json"]["schema"]
    assert ok == {"$ref": "#/components/schemas/PostOut"}
    assert create["security"] == [{"TokenAuth": ["posts:write"]}]
    assert {"401", "403"} <= set(create["responses"])


def test_security_scheme_is_described_from_the_provider() -> None:
    components = make_app().openapi()["components"]
    assert components["securitySchemes"] == {
        "TokenAuth": {"type": "http", "scheme": "bearer"}
    }


def test_none_return_has_no_content_and_lists_are_arrays() -> None:
    paths = make_app().openapi()["paths"]
    publish = paths["/posts/{post_id}/publish"]["post"]["responses"]["204"]
    assert "content" not in publish
    listing = paths["/posts"]["get"]["responses"]["200"]["content"]
    assert listing["application/json"]["schema"]["type"] == "array"
    stats_schema = paths["/stats"]["get"]["responses"]["200"]["content"]
    assert stats_schema["application/json"]["schema"] == {
        "type": "object",
        "additionalProperties": {"type": "integer"},
    }


def test_schema_components_carry_constraints_nullability_and_requirements() -> None:
    schemas = make_app().openapi()["components"]["schemas"]
    create = schemas["PostCreate"]
    assert create["required"] == ["title"]
    assert create["additionalProperties"] is False
    assert create["description"] == "Input for creating a post."
    assert create["properties"]["title"] == {
        "type": "string",
        "minLength": 1,
        "maxLength": 160,
    }
    assert create["properties"]["stars"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": 5,
    }
    assert create["properties"]["tags"] == {
        "type": "array",
        "items": {"type": "string"},
        "maxItems": 3,
    }
    assert create["properties"]["subtitle"] == {
        "anyOf": [{"type": "string"}, {"type": "null"}]
    }
    assert schemas["PostOut"]["properties"]["author"] == {
        "$ref": "#/components/schemas/AuthorOut"
    }
    assert "ValidationError" in schemas


def test_recursive_schemas_do_not_loop() -> None:
    tree_schema = make_app().openapi()["components"]["schemas"]["Tree"]
    assert tree_schema["properties"]["children"]["items"] == {
        "$ref": "#/components/schemas/Tree"
    }


def test_same_name_different_schemas_are_rejected() -> None:
    clash = type("PostOut", (Schema,), {"__annotations__": {"x": int}})

    @get("/clash")
    def other(request: Request) -> clash:  # type: ignore[valid-type]
        raise NotImplementedError

    app = make_app()
    app.include(other)
    with pytest.raises(RegistrationError, match="PostOut"):
        app.openapi()


async def test_document_is_served_when_a_url_is_configured(pool: Pool) -> None:
    client = await pool.start(make_app(openapi_url="/openapi.json"))
    reply = await client.request("GET", "/openapi.json")
    assert reply.status == 200
    assert reply.body["info"]["title"] == "Blog"


async def test_document_is_not_served_by_default(pool: Pool) -> None:
    client = await pool.start(make_app())
    assert (await client.request("GET", "/openapi.json")).status == 404


def test_document_url_cannot_shadow_a_route() -> None:
    with pytest.raises(RegistrationError, match="already used"):
        make_app(openapi_url="/health").asgi()


class Pattern(Schema):
    slug: Annotated[str, Field(pattern="[a-z]+")]


@get("/me", access=editor)
async def me(request: Request, principal: Principal[str]) -> None: ...


@get("/remove/{item}", status=200)
async def remove(request: Request, item: str) -> None: ...


@post("/pat")
async def pat(request: Request, data: Pattern) -> None: ...


def test_principal_is_not_documented_as_an_input() -> None:
    app = HinokiAPI()
    app.include(me)
    operation = app.openapi()["paths"]["/me"]["get"]
    assert "parameters" not in operation
    assert "422" not in operation["responses"]
    validate(app.openapi())


def test_none_returning_actions_are_documented_as_204() -> None:
    app = HinokiAPI()
    app.include(remove)
    responses = app.openapi()["paths"]["/remove/{item}"]["get"]["responses"]
    assert "204" in responses
    assert "200" not in responses


def test_patterns_are_documented_anchored_like_the_runtime_matches_them() -> None:
    app = HinokiAPI()
    app.include(pat)
    slug = app.openapi()["components"]["schemas"]["Pattern"]["properties"]["slug"]
    assert slug["pattern"] == "^(?:[a-z]+)$"
