"""Generated CRUD: declaration checks (no database) and behaviour (PostgreSQL)."""

import json
import os
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID, uuid4

import pytest
from openapi_spec_validator import validate

from hinoki import (
    API,
    UNSET,
    Access,
    BearerAuth,
    Caller,
    CrudAccess,
    Field,
    Hinoki,
    Schema,
    Unset,
    crud,
    http,
)
from hinoki.db import Database, DatabaseModule
from hinoki.errors import Forbidden, RegistrationError
from hinoki.http.request import Request
from hinoki.orm import Model, QuerySet, field, use_database
from hinoki.pagination import Page, PageNumberPagination, PageParams
from hinoki.resources import _CrudConfig
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
    async def resolve_token(self, token: str) -> Caller[str] | None:
        name = token.strip()
        scopes = frozenset() if name == "viewer" else frozenset({"memo:write"})
        return Caller(id=name, user=name, permissions=scopes)


auth = Tokens()
writer = Access(auth=auth, permissions=["memo:write"])


def memo_crud(
    path: str,
    access: Access | CrudAccess = CrudAccess(default=writer, list=None, retrieve=None),  # noqa: B008
    **changes: Any,
) -> Any:
    options: dict[str, Any] = {
        "read": MemoOut,
        "create": MemoIn,
        "update": MemoPatch,
        "delete": True,
        "owner": "owner",
        "identity_key": str,
    }
    options.update(changes)
    return crud(Memo, path=path, access=access, **options)


Memos = memo_crud("/memos").api()


@crud(Memo, path="/backwards", read=MemoOut)
class Backwards(API):
    def get_queryset(self, principal: Caller[Any] | None) -> QuerySet[Memo]:
        return Memo.all().order_by("-title")


class TightOut(Schema):
    id: UUID
    owner: str
    title: Annotated[str, Field(max_length=3)]
    body: str


@crud(Memo, path="/tight", read=TightOut)
class Tight(API):
    pass


@memo_crud("/hooked")
class HookedMemos(API):
    async def store(self, values: dict[str, Any], principal: Caller[Any]) -> Any:
        values["title"] = f"hook:{values['title']}"
        return await Memo.acreate(**values)


@memo_crud("/private", access=writer)
class PrivateMemos(API):
    def get_queryset(self, principal: Caller[Any] | None) -> QuerySet[Memo]:
        assert principal is not None
        return Memo.filter(owner=principal.user)


@memo_crud("/unsafe-hooks")
class UnsafeHooks(API):
    async def store(self, values: dict[str, Any], principal: Caller[Any]) -> Any:
        item = await Memo.acreate(**values)
        await Memo.aupdate_where({"id": item.id}, {"owner": "bob"})
        return item

    async def change(
        self,
        item: Memo,
        scoped: QuerySet[Memo],
        values: dict[str, Any],
        principal: Caller[Any],
    ) -> Memo:
        await Memo.aupdate_where({"id": item.id}, {"owner": "bob"})
        return item


@memo_crud(
    "/sync-hooks", pagination=PageNumberPagination(page_size=100, max_page_size=100)
)
class SyncHooks(API):
    def get_queryset(self, principal: Caller[Any] | None) -> QuerySet[Memo]:
        del principal
        return Memo.all()

    def written_queryset(self, principal: Caller[Any] | None) -> QuerySet[Memo]:
        assert principal is not None
        return Memo.all()

    def to_schema(self, item: Memo) -> MemoOut:
        return MemoOut(id=item.id, owner=item.owner, title=item.title, body=item.body)

    def store(self, values: dict[str, Any], principal: Caller[Any]) -> Memo:
        return Memo.create(**values)

    def change(
        self,
        item: Memo,
        scoped: QuerySet[Memo],
        values: dict[str, Any],
        principal: Caller[Any],
    ) -> Memo:
        if values:
            scoped.update(values)
        return item

    def remove(
        self, item: Memo, scoped: QuerySet[Memo], principal: Caller[Any]
    ) -> bool:
        return scoped.delete() > 0


def app_for(*apis: type[Any], url: str | None = None) -> Hinoki:
    app = Hinoki(title="Memos", openapi_url="/openapi.json")
    app.include(*apis)
    if url:
        app.install(DatabaseModule(url, max_size=3))
    return app


def declare(**changes: Any) -> type[API]:
    result: type[API] = crud(Memo, path="/x", read=MemoOut, **changes).api()
    return result


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"create": MemoIn}, "need access"),
        ({"delete": True}, "need access"),
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
        _CrudConfig(model=Memo, read=Wrong).check()


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


def test_class_configuration_composes_with_custom_routes_and_hooks() -> None:
    @crud(
        Memo,
        path="/posts",
        read=MemoOut,
        create=MemoIn,
        access=writer,
        owner="owner",
        identity_key=str,
    )
    class Posts(API):
        async def store(self, values: dict[str, Any], principal: Caller[Any]) -> Any:
            values["title"] = f"hook:{values['title']}"
            return await Memo.acreate(**values)

        @http.get("/health")
        async def health(self, request: Request) -> dict[str, str]:
            return {"status": "ok"}

    routes = Posts.routes()
    assert Posts.routes()
    assert {route.handler.__name__ for route in routes} == {
        "list",
        "retrieve",
        "create",
        "health",
    }
    assert all(route.path.startswith("/posts") for route in routes)
    assert "/posts/health" in app_for(Posts).openapi()["paths"]


def test_class_api_custom_route_can_replace_generated_action() -> None:
    @crud(Memo, path="/custom", read=MemoOut)
    class Custom(API):
        @http.get("/", replaces="list")
        async def listing(self, request: Request) -> dict[str, str]:
            return {"custom": "yes"}

    assert {route.handler.__name__ for route in Custom.routes()} == {
        "listing",
        "retrieve",
    }


def test_a_handwritten_action_name_must_not_shadow_a_generated_one() -> None:
    with pytest.raises(RegistrationError, match="both generated and handwritten"):

        @crud(Memo, path="/no-listing", read=MemoOut)
        class NoListing(API):
            def list(self) -> None: ...


def test_reads_only_resources_generate_only_reads() -> None:
    api = declare()
    assert {r.handler.__name__ for r in api.routes()} == {"list", "retrieve"}


def test_replaces_must_name_an_action() -> None:
    @crud(Memo, path="/y", read=MemoOut)
    class Bad(API):
        @http.get("/", replaces="nothing")
        async def mine(self, request: Request) -> dict[str, str]:
            return {}

    with pytest.raises(RegistrationError, match="nothing"):
        Bad.routes()


def test_replaces_swaps_a_generated_action_for_a_handwritten_route() -> None:
    @crud(Memo, path="/z", read=MemoOut, omit=("retrieve",))
    class Custom(API):
        @http.get("/", replaces="list", pagination=PageNumberPagination())
        async def mine(self, request: Request, page: PageParams) -> Page[MemoOut]:
            return Page([], 1, 10, 0)

        @http.get("/{item_id}")
        async def retrieve_mine(
            self, request: Request, item_id: UUID
        ) -> dict[str, str]:
            return {"custom": "yes"}

    assert sorted(r.handler.__name__ for r in Custom.routes()) == [
        "mine",
        "retrieve_mine",
    ]
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
    yield await pool.start(
        app_for(
            Memos,
            Backwards,
            HookedMemos,
            PrivateMemos,
            UnsafeHooks,
            SyncHooks,
            Tight,
            url=URL,
        )
    )
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
async def test_a_stored_row_the_read_schema_refuses_is_a_server_error(
    memos: Client,
) -> None:
    key = uuid4()
    assert URL is not None
    admin = await Database.connect(URL, max_size=1)
    await admin.execute(
        "INSERT INTO crud_memos VALUES ($1, 'ann', 'too long', '')", key
    )
    admin.close()
    single = await memos.request("GET", f"/tight/{key}")
    listing = await memos.request("GET", "/tight")
    assert (single.status, listing.status) == (500, 500)
    assert "too long" not in f"{single.body}{listing.body}"


@needs_database
async def test_store_hook_runs_inside_generated_action(memos: Client) -> None:
    created = await memos.request(
        "POST", "/hooked", body(title="Hello"), content_type=JSON, headers=ANN
    )
    assert created.status == 201
    assert created.body["title"] == "hook:Hello"


@needs_database
async def test_all_crud_hooks_accept_sync_definitions(memos: Client) -> None:
    created = await memos.request(
        "POST",
        "/sync-hooks",
        body(title="sync"),
        content_type=JSON,
        headers=ANN,
    )
    assert created.status == 201
    key = created.body["id"]
    assert (await memos.request("GET", "/sync-hooks", headers=ANN)).body["total"] == 1
    assert (await memos.request("GET", f"/sync-hooks/{key}", headers=ANN)).body[
        "title"
    ] == "sync"
    updated = await memos.request(
        "PATCH",
        f"/sync-hooks/{key}",
        body(title="updated"),
        content_type=JSON,
        headers=ANN,
    )
    assert updated.body["title"] == "updated"
    assert (
        await memos.request("DELETE", f"/sync-hooks/{key}", headers=ANN)
    ).status == 204


@needs_database
async def test_sync_schema_hook_does_not_saturate_executor_for_full_page(
    memos: Client,
) -> None:
    assert URL is not None
    admin = await Database.connect(URL, max_size=1)
    for index in range(100):
        await admin.execute(
            "INSERT INTO crud_memos (id, owner, title, body) VALUES ($1, $2, $3, $4)",
            uuid4(),
            "ann",
            f"note-{index}",
            "",
        )
    admin.close()

    response = await memos.request(
        "GET", "/sync-hooks", query="page_size=100", headers=ANN
    )

    assert response.status == 200, response.body
    assert response.body["total"] == 100
    assert len(response.body["items"]) == 100


@needs_database
async def test_get_queryset_receives_caller_and_scopes_private_reads(
    memos: Client,
) -> None:
    ann = (
        await memos.request(
            "POST", "/memos", body(title="Ann"), content_type=JSON, headers=ANN
        )
    ).body
    bob = (
        await memos.request(
            "POST", "/memos", body(title="Bob"), content_type=JSON, headers=BOB
        )
    ).body
    assert (await memos.request("GET", "/private", headers=ANN)).body["total"] == 1
    assert (
        await memos.request("GET", f"/private/{bob['id']}", headers=ANN)
    ).status == 404
    assert (
        await memos.request("GET", f"/private/{ann['id']}", headers=ANN)
    ).status == 200


@needs_database
async def test_hooks_cannot_commit_server_assigned_owner_changes(memos: Client) -> None:
    assert URL is not None
    created = await memos.request(
        "POST",
        "/unsafe-hooks",
        body(title="new"),
        content_type=JSON,
        headers=ANN,
    )
    assert created.status == 500

    original = (
        await memos.request(
            "POST", "/memos", body(title="original"), content_type=JSON, headers=ANN
        )
    ).body
    patch = await memos.request(
        "PATCH",
        f"/unsafe-hooks/{original['id']}",
        body(title="changed"),
        content_type=JSON,
        headers=ANN,
    )
    assert patch.status == 500
    admin = await Database.connect(URL, max_size=1)
    try:
        rows = await admin.fetch(
            "SELECT owner, title FROM crud_memos WHERE id = $1",
            UUID(original["id"]),
        )
        assert rows[0]["owner"] == "ann"
        assert rows[0]["title"] == "original"
        assert await admin.fetch("SELECT id FROM crud_memos WHERE title = 'new'") == []
    finally:
        admin.close()


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


@crud(
    Hidden,
    path="/scoped",
    read=HiddenOut,
    update=HiddenPatch,
    delete=True,
    access=CrudAccess(default=writer, list=None, retrieve=None),
    owner="owner",
    identity_key=str,
)
class Scoped(API):
    async def get_queryset(self, principal: Caller[Any] | None) -> QuerySet[Hidden]:
        return Hidden.filter(hidden=False)


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
            _CrudConfig(
                model=Hidden, read=HiddenOut, update=schema, access=writer
            ).check()


def test_replaces_rejects_duplicates_chains_and_loose_functions() -> None:
    @crud(Memo, path="/t", read=MemoOut)
    class Twice(API):
        @http.get("/a", replaces="list")
        async def a(self, request: Request) -> dict[str, str]:
            return {}

        @http.get("/b", replaces="list")
        async def b(self, request: Request) -> dict[str, str]:
            return {}

    with pytest.raises(RegistrationError, match="once"):
        Twice.routes()

    @crud(Memo, path="/u", read=MemoOut)
    class Chain(API):
        @http.get("/a", replaces="list")
        async def a(self, request: Request) -> dict[str, str]:
            return {}

        @http.get("/b", replaces="a")
        async def b(self, request: Request) -> dict[str, str]:
            return {}

    with pytest.raises(RegistrationError, match="never a route"):
        Chain.routes()

    @http.get("/loose", replaces="list")
    async def loose(request: Request) -> dict[str, str]:
        return {}

    with pytest.raises(RegistrationError, match="inside an API class"):
        Hinoki().include(loose)


def test_a_required_column_is_not_covered_by_an_optional_create_field() -> None:
    class Loose(Schema):
        title: str | Unset = UNSET

    resource = _CrudConfig(
        model=Memo, read=MemoOut, create=Loose, access=writer, owner="owner"
    )
    with pytest.raises(RegistrationError, match="required columns"):
        resource.check()


def test_a_caller_without_an_owner_value_may_change_nothing() -> None:
    resource = _CrudConfig(
        model=Memo, read=MemoOut, owner="owner", identity_key=lambda _: None
    )
    service = Caller(id="svc", user="svc", permissions=frozenset())
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
        ann = Caller(id="ann", user="ann", permissions=frozenset())
        api: Any = Scoped()
        # Zero rows because the row vanished is a 404; because it is owned by
        # someone else (checked against a still visible row) it is a 403.
        with pytest.raises(Hidden.NotFound):
            await api.refuse(hidden, ann)
        assert isinstance(await api.refuse(shown, ann), Forbidden)
        assert len(await admin.fetch("SELECT id FROM crud_hidden")) == 2
