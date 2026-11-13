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
from hinoki.httperrors import Forbidden
from hinoki.orm import Model, field, use_database
from hinoki.pagination import Page, PageNumberPagination, PageParams
from hinoki.request import Request
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


BACKWARDS = Resource(
    model=Memo, read=MemoOut, queryset=lambda: Memo.all().order_by("-title")
)


class Backwards(CrudAPI, path="/backwards", resource=BACKWARDS):
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
        async def mine(self, request: Request) -> dict[str, str]:
            return {}

    with pytest.raises(RegistrationError, match="nothing"):
        Bad.routes()


def test_replaces_and_same_name_overrides_swap_generated_actions() -> None:
    class Custom(CrudAPI, path="/z", resource=Resource(model=Memo, read=MemoOut)):
        @get("/", replaces="list", pagination=PageNumberPagination())
        async def mine(self, request: Request, page: PageParams) -> Page[MemoOut]:
            return Page([], 1, 10, 0)

        @get("/{item_id}")
        async def retrieve(self, request: Request, item_id: UUID) -> dict[str, str]:
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
    yield await pool.start(app_for(Memos, Backwards, url=URL))
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


class Hidden(Model, table="crud_hidden"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    owner: str = field(max_length=20)
    hidden: bool = False


class HiddenOut(Schema):
    id: UUID
    owner: str
    hidden: bool


class HiddenPatch(Schema):
    hidden: bool | Unset = UNSET


def visible() -> Any:
    return Hidden.filter(hidden=False)


SCOPED = Resource(
    model=Hidden,
    read=HiddenOut,
    update=HiddenPatch,
    deletable=True,
    access=writer,
    owner="owner",
    identity_key=str,
    queryset=visible,
)


class Scoped(CrudAPI, path="/scoped", resource=SCOPED):
    pass


@pytest.fixture
async def scoped(pool: Pool) -> AsyncIterator[tuple[Client, Database]]:
    assert URL is not None
    admin = await Database.connect(URL, max_size=1)
    await admin.execute("DROP TABLE IF EXISTS crud_hidden")
    await admin.execute(
        "CREATE TABLE crud_hidden (id uuid PRIMARY KEY, owner varchar(20) NOT NULL, "
        "hidden boolean NOT NULL)"
    )
    yield await pool.start(app_for(Scoped, url=URL)), admin
    await admin.execute("DROP TABLE IF EXISTS crud_hidden")
    admin.close()


@needs_database
async def test_rows_outside_the_base_query_are_not_found_for_every_action(
    scoped: tuple[Client, Database],
) -> None:
    client, admin = scoped
    mine, theirs = uuid4(), uuid4()
    for key, owner in ((mine, "ann"), (theirs, "bob")):
        await admin.execute("INSERT INTO crud_hidden VALUES ($1, $2, true)", key, owner)
    for key in (mine, theirs):
        path = f"/scoped/{key}"
        assert (await client.request("GET", path)).status == 404
        patch = await client.request(
            "PATCH", path, body(hidden=False), content_type=JSON, headers=ANN
        )
        assert patch.status == 404
        assert (await client.request("DELETE", path, headers=ANN)).status == 404
    rows = await admin.fetch("SELECT id FROM crud_hidden")
    assert len(rows) == 2


def test_update_schemas_cannot_carry_the_key_or_loud_defaults() -> None:
    class WithKey(Schema):
        id: UUID | Unset = UNSET

    class Loud(Schema):
        hidden: bool = False

    class Nullable(Schema):
        owner: str | None | Unset = UNSET

    for schema, message in (
        (WithKey, "must not contain"),
        (Loud, "default to UNSET"),
        (Nullable, "accepts null"),
    ):
        with pytest.raises(RegistrationError, match=message):
            Resource(model=Hidden, read=HiddenOut, update=schema, access=writer).check()


def test_an_inherited_override_survives_and_a_new_resource_regenerates() -> None:
    class Base(CrudAPI):
        @get("/{item_id}")
        async def retrieve(self, request: Request, item_id: UUID) -> dict[str, str]:
            return {"custom": "yes"}

    class Child(Base, path="/c", resource=Resource(model=Memo, read=MemoOut)):
        pass

    assert Child.retrieve is Base.retrieve

    class Other(Child, resource=Resource(model=Memo, read=MemoOut)):
        pass

    assert Other.retrieve is Base.retrieve


def test_replaces_rejects_duplicates_chains_and_loose_functions() -> None:
    class Twice(CrudAPI, path="/t", resource=Resource(model=Memo, read=MemoOut)):
        @get("/a", replaces="list")
        async def a(self, request: Request) -> dict[str, str]:
            return {}

        @get("/b", replaces="list")
        async def b(self, request: Request) -> dict[str, str]:
            return {}

    with pytest.raises(RegistrationError, match="once"):
        Twice.routes()

    class Chain(CrudAPI, path="/u", resource=Resource(model=Memo, read=MemoOut)):
        @get("/a", replaces="list")
        async def a(self, request: Request) -> dict[str, str]:
            return {}

        @get("/b", replaces="a")
        async def b(self, request: Request) -> dict[str, str]:
            return {}

    with pytest.raises(RegistrationError, match="never a route"):
        Chain.routes()

    @get("/loose", replaces="list")
    async def loose(request: Request) -> dict[str, str]:
        return {}

    with pytest.raises(RegistrationError, match="inside an API class"):
        HinokiAPI().include(loose)


def test_a_narrower_resource_drops_the_parent_write_routes() -> None:
    class Public(Memos, path="/public", resource=Resource(model=Memo, read=MemoOut)):
        pass

    assert {r.handler.__name__ for r in Public.routes()} == {"list", "retrieve"}
    assert {r.handler.__name__ for r in Memos.routes()} >= {"update", "delete"}


def test_a_required_column_is_not_covered_by_an_optional_create_field() -> None:
    class Loose(Schema):
        title: str | Unset = UNSET

    resource = Resource(
        model=Memo, read=MemoOut, create=Loose, access=writer, owner="owner"
    )
    with pytest.raises(RegistrationError, match="required columns"):
        resource.check()


def test_a_caller_without_an_owner_value_may_change_nothing() -> None:
    resource = Resource(
        model=Memo, read=MemoOut, owner="owner", identity_key=lambda _: None
    )
    service = Principal(subject="svc", identity="svc", scopes=frozenset())
    with pytest.raises(Forbidden):
        resource.owner_of(service)


@needs_database
async def test_list_keeps_the_ordering_of_the_base_query(memos: Client) -> None:
    for title in ("a", "c", "b"):
        await memos.request(
            "POST", "/memos", body(title=title), content_type=JSON, headers=ANN
        )
    listing = (await memos.request("GET", "/backwards")).body["items"]
    assert [m["title"] for m in listing] == ["c", "b", "a"]


@needs_database
async def test_a_change_that_hides_the_row_still_answers_and_persists(
    scoped: tuple[Client, Database],
) -> None:
    client, admin = scoped
    key = uuid4()
    await admin.execute("INSERT INTO crud_hidden VALUES ($1, 'ann', false)", key)
    patch = await client.request(
        "PATCH", f"/scoped/{key}", body(hidden=True), content_type=JSON, headers=ANN
    )
    assert (patch.status, patch.body["hidden"]) == (200, True)
    rows = await admin.fetch("SELECT hidden FROM crud_hidden WHERE id = $1", key)
    assert rows[0]["hidden"] is True


@needs_database
async def test_writes_carry_the_base_query_and_report_missing_as_404(
    scoped: tuple[Client, Database],
) -> None:
    _, admin = scoped
    hidden, shown = uuid4(), uuid4()
    await admin.execute("INSERT INTO crud_hidden VALUES ($1, 'ann', true)", hidden)
    await admin.execute("INSERT INTO crud_hidden VALUES ($1, 'ann', false)", shown)
    with use_database(admin):
        ann = Principal(subject="ann", identity="ann", scopes=frozenset())
        # A row hidden after the guard is not deleted by the statement itself.
        assert await SCOPED.scope(hidden, ann).adelete() == 0
        assert len(await admin.fetch("SELECT id FROM crud_hidden")) == 2
        # Zero rows because the row vanished is a 404; because it is owned by
        # someone else (checked against a still visible row) it is a 403.
        with pytest.raises(Hidden.NotFound):
            await SCOPED.refused(hidden)
        assert isinstance(await SCOPED.refused(shown), Forbidden)
