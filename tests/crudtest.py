"""Generated CRUD: declaration checks (no database) and behaviour (PostgreSQL)."""

import json
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from openapi_spec_validator import validate

from hinoki import (
    UNSET,
    Access,
    BearerAuth,
    HinokiAPI,
    Principal,
    RegistrationError,
    Schema,
    Unset,
    get,
)
from hinoki.crud import CrudAPI, Resource
from hinoki.db import Database, DatabaseModule
from hinoki.orm import Model, field
from hinoki.pagination import Page, PageNumberPagination, PageParams
from tests.harness import Client, Pool

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
needs_database = pytest.mark.skipif(
    URL is None, reason="HINOKI_TEST_DATABASE_URL is not set"
)
JSON = "application/json"
ANN = {"authorization": "Bearer ann"}
BOB = {"authorization": "Bearer bob"}
VIEWER = {"authorization": "Bearer viewer"}


class Memo(Model, table="crud_memos"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    owner: str = field(max_length=20)
    title: str = field(max_length=40)
    body: str = ""


class MemoOut(Schema):
    id: UUID
    owner: str
    title: str
    body: str


class MemoIn(Schema):
    title: str
    body: str = ""


class MemoPatch(Schema):
    title: str | Unset = UNSET
    body: str | Unset = UNSET


class Tokens(BearerAuth[str]):
    async def resolve_token(self, token: str) -> Principal[str] | None:
        name = token.strip()
        scopes = frozenset() if name == "viewer" else frozenset({"memo:write"})
        return Principal(subject=name, identity=name, scopes=scopes)


auth = Tokens()
writer = Access(auth=auth, scopes=["memo:write"])
FULL = Resource(
    model=Memo,
    read=MemoOut,
    create=MemoIn,
    update=MemoPatch,
    deletable=True,
    access=writer,
    owner="owner",
    identity_key=str,
)


class Memos(CrudAPI, path="/memos", resource=FULL):
    pass


def app_for(*apis: type[Any], url: str | None = None) -> HinokiAPI:
    app = HinokiAPI(title="Memos", openapi_url="/openapi.json")
    app.include(*apis)
    if url:
        app.install(DatabaseModule(url, max_size=3))
    return app


def declare(**changes: Any) -> type[CrudAPI]:
    resource = Resource(model=Memo, read=MemoOut, **changes)
    return type("Declared", (CrudAPI,), {}, path="/x", resource=resource)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"create": MemoIn}, "need access"),
        ({"deletable": True}, "need access"),
        ({"access": writer, "owner": "nope", "create": MemoIn}, "no column 'nope'"),
        ({"owner": "owner", "create": MemoIn, "public_writes": True}, "access policy"),
        (
            {"access": writer, "owner": "owner", "create": MemoOut},
            "server assigns",
        ),
        ({"access": writer, "create": MemoIn, "assign": {}}, "required columns"),
    ],
)
def test_unsafe_or_incomplete_resources_fail_at_declaration(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(RegistrationError, match=message):
        declare(**changes)


def test_schema_fields_must_exist_on_the_model() -> None:
    class Wrong(Schema):
        id: UUID
        nonsense: str

    with pytest.raises(RegistrationError, match="nonsense"):
        Resource(model=Memo, read=Wrong).check()


def test_generated_actions_are_real_routes_in_the_documentation() -> None:
    document = app_for(Memos).openapi()
    validate(document)
    assert set(document["paths"]) == {"/memos", "/memos/{item_id}"}
    assert set(document["paths"]["/memos"]) == {"get", "post"}
    assert set(document["paths"]["/memos/{item_id}"]) == {"get", "patch", "delete"}
    create = document["paths"]["/memos"]["post"]
    assert "201" in create["responses"]
    assert create["security"]
    assert "security" not in document["paths"]["/memos"]["get"]


def test_reads_only_resources_generate_only_reads() -> None:
    api = declare()
    assert {r.handler.__name__ for r in api.routes()} == {"list", "retrieve"}


def test_replaces_must_name_an_action() -> None:
    class Bad(CrudAPI, path="/y", resource=Resource(model=Memo, read=MemoOut)):
        @get("/", replaces="nothing")
        async def mine(self) -> dict[str, str]:
            return {}

    with pytest.raises(RegistrationError, match="nothing"):
        Bad.routes()


def test_replaces_and_same_name_overrides_swap_generated_actions() -> None:
    class Custom(CrudAPI, path="/z", resource=Resource(model=Memo, read=MemoOut)):
        @get("/", replaces="list", pagination=PageNumberPagination())
        async def mine(self, page: PageParams) -> Page[MemoOut]:
            return Page([], 1, 10, 0)

        @get("/{item_id}")
        async def retrieve(self, item_id: UUID) -> dict[str, str]:
            return {"custom": "yes"}

    assert sorted(r.handler.__name__ for r in Custom.routes()) == ["mine", "retrieve"]
    assert app_for(Custom).openapi()


@pytest.fixture
async def memos(pool: Pool) -> AsyncIterator[Client]:
    assert URL is not None
    admin = await Database.connect(URL, max_size=1)
    await admin.execute("DROP TABLE IF EXISTS crud_memos")
    await admin.execute(
        "CREATE TABLE crud_memos (id uuid PRIMARY KEY, owner varchar(20) NOT NULL, "
        "title varchar(40) NOT NULL, body text NOT NULL)"
    )
    yield await pool.start(app_for(Memos, url=URL))
    await admin.execute("DROP TABLE IF EXISTS crud_memos")
    admin.close()


def body(**values: Any) -> bytes:
    return json.dumps(values).encode()


@needs_database
async def test_create_assigns_the_owner_and_reads_back(memos: Client) -> None:
    created = await memos.request(
        "POST", "/memos", body(title="Hello"), content_type=JSON, headers=ANN
    )
    assert created.status == 201
    assert (created.body["owner"], created.body["title"], created.body["body"]) == (
        "ann",
        "Hello",
        "",
    )
    fetched = await memos.request("GET", f"/memos/{created.body['id']}")
    assert fetched.body == created.body
    listing = (await memos.request("GET", "/memos")).body
    assert (listing["total"], [m["id"] for m in listing["items"]]) == (
        1,
        [created.body["id"]],
    )


@needs_database
async def test_clients_cannot_send_server_assigned_fields(memos: Client) -> None:
    reply = await memos.request(
        "POST",
        "/memos",
        body(title="x", owner="bob"),
        content_type=JSON,
        headers=ANN,
    )
    assert reply.status == 422


@needs_database
async def test_authentication_scopes_and_ownership(memos: Client) -> None:
    anonymous = await memos.request(
        "POST", "/memos", body(title="x"), content_type=JSON
    )
    assert anonymous.status == 401
    viewer = await memos.request(
        "POST", "/memos", body(title="x"), content_type=JSON, headers=VIEWER
    )
    assert viewer.status == 403
    mine = (
        await memos.request(
            "POST", "/memos", body(title="mine"), content_type=JSON, headers=ANN
        )
    ).body["id"]
    path = f"/memos/{mine}"
    patch = await memos.request(
        "PATCH", path, body(title="t"), content_type=JSON, headers=BOB
    )
    assert patch.status == 403
    assert (await memos.request("DELETE", path, headers=BOB)).status == 403
    assert (await memos.request("GET", path)).body["title"] == "mine"


@needs_database
async def test_patch_changes_only_the_fields_that_were_sent(memos: Client) -> None:
    key = (
        await memos.request(
            "POST",
            "/memos",
            body(title="one", body="text"),
            content_type=JSON,
            headers=ANN,
        )
    ).body["id"]
    path = f"/memos/{key}"
    changed = await memos.request(
        "PATCH", path, body(title="two"), content_type=JSON, headers=ANN
    )
    assert (changed.body["title"], changed.body["body"]) == ("two", "text")
    untouched = await memos.request(
        "PATCH", path, body(), content_type=JSON, headers=ANN
    )
    assert untouched.body == changed.body
    invalid = await memos.request(
        "PATCH", path, body(title=3), content_type=JSON, headers=ANN
    )
    assert invalid.status == 422


@needs_database
async def test_delete_and_missing_rows(memos: Client) -> None:
    key = (
        await memos.request(
            "POST", "/memos", body(title="gone"), content_type=JSON, headers=ANN
        )
    ).body["id"]
    path = f"/memos/{key}"
    assert (await memos.request("DELETE", path, headers=ANN)).status == 204
    assert (await memos.request("GET", path)).status == 404
    assert (await memos.request("DELETE", path, headers=ANN)).status == 404
    missing = f"/memos/{uuid4()}"
    patch = await memos.request(
        "PATCH", missing, body(title="t"), content_type=JSON, headers=ANN
    )
    assert patch.status == 404
    assert (await memos.request("GET", "/memos/not-a-uuid")).status == 422
