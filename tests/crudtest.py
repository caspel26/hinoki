"""Generated CRUD: declaration checks (no database) and behaviour (PostgreSQL)."""

import json
import os
from collections.abc import AsyncIterator
from operator import attrgetter
from pathlib import Path
from types import SimpleNamespace
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
    from_user,
    http,
)
from hinoki.db import Database, DatabaseModule
from hinoki.errors import Forbidden, RegistrationError
from hinoki.http.request import Request
from hinoki.orm import Model, QuerySet, field, use_database
from hinoki.pagination import Page, PageNumberPagination, PageParams
from hinoki.resources import UserAttr, _CrudConfig
from hinoki.runtime.crudplan import assign_is_plain
from hinoki.testing import ASGIClient
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

        @http.get("/health", access=None)
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
        owner: str | Unset | None = UNSET

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


class _BaseUser:
    def __init__(self, uid: str, org_id: str) -> None:
        self.id = uid
        self.org_id = org_id
        self.org = SimpleNamespace(id=org_id)


class _MemberUser(_BaseUser):
    pass


class _OtherUser:
    pass


class _MemberTokens(BearerAuth[_MemberUser]):
    async def resolve_token(self, token: str) -> Caller[_MemberUser] | None:
        name = token.strip()
        user = _MemberUser(name, f"org-{name}")
        return Caller(id=name, user=user, permissions=frozenset({"memo:write"}))


class _UnparamTokens(BearerAuth[Any]):
    async def resolve_token(self, token: str) -> Caller[Any] | None:
        return Caller(id=token, user=_MemberUser(token, "org"), permissions=frozenset())


_member_access = Access(auth=_MemberTokens(), permissions=["memo:write"])
_unparam_access = Access(auth=_UnparamTokens())


def test_hook_non_none_caller_allowed_on_protected_crud() -> None:
    @crud(
        Memo,
        path="/protected-hooks",
        read=MemoOut,
        create=MemoIn,
        delete=True,
        access=_member_access,
        owner="owner",
    )
    class ProtectedMemos(API):
        def get_queryset(self, caller: Caller[_MemberUser]) -> QuerySet[Memo]:
            return Memo.filter(owner=caller.user.id)

        def written_queryset(self, caller: Caller[_BaseUser]) -> QuerySet[Memo]:
            return Memo.filter(owner=caller.user.id)

        async def remove(
            self, item: Memo, scoped: QuerySet[Memo], caller: Caller[_MemberUser]
        ) -> bool:
            return bool(await scoped.filter(owner=caller.user.id).adelete())

    assert ProtectedMemos.routes()


def test_hook_non_none_caller_rejected_on_public_action() -> None:
    class PublicMemoIn(Schema):
        owner: str
        title: str

    with pytest.raises(
        RegistrationError,
        match=r"PublicHookMemos\.get_queryset.*non-None Caller.*list, retrieve",
    ):

        @crud(
            Memo,
            path="/public-hook",
            read=MemoOut,
            create=MemoIn,
            access=CrudAccess(default=_member_access, list=None, retrieve=None),
            owner="owner",
        )
        class PublicHookMemos(API):
            def get_queryset(self, caller: Caller[_MemberUser]) -> QuerySet[Memo]:
                return Memo.filter(owner=caller.user.id)

    with pytest.raises(
        RegistrationError,
        match=r"PublicWriteHook\.store.*non-None Caller.*create",
    ):

        @crud(
            Memo,
            path="/public-write-hook",
            read=MemoOut,
            create=PublicMemoIn,
            public_writes=True,
        )
        class PublicWriteHook(API):
            async def store(
                self, values: dict[str, Any], caller: Caller[_MemberUser]
            ) -> Memo:
                return await Memo.acreate(**values)


def test_hook_non_none_caller_allowed_when_public_reads_omitted() -> None:
    @crud(
        Memo,
        path="/writes-only-hook",
        read=MemoOut,
        create=MemoIn,
        delete=True,
        access=_member_access,
        owner="owner",
        omit=("list", "retrieve"),
    )
    class WritesOnlyMemos(API):
        def get_queryset(self, caller: Caller[_MemberUser]) -> QuerySet[Memo]:
            return Memo.filter(owner=caller.user.id)

    assert WritesOnlyMemos.routes()


def test_hook_caller_type_mismatch_raises_registration_error() -> None:
    with pytest.raises(
        RegistrationError,
        match=r"WrongTypeMemos\.get_queryset.*Caller\[_OtherUser\].*Caller\[_MemberUser\]",
    ):

        @crud(
            Memo,
            path="/wrong-type",
            read=MemoOut,
            access=_member_access,
        )
        class WrongTypeMemos(API):
            def get_queryset(self, caller: Caller[_OtherUser]) -> QuerySet[Memo]:
                return Memo.all()

    with pytest.raises(
        RegistrationError,
        match=r"WrongOptionalMemos\.get_queryset.*Caller\[_OtherUser\].*Caller\[_MemberUser\]",
    ):

        @crud(
            Memo,
            path="/wrong-optional-type",
            read=MemoOut,
            delete=True,
            access=CrudAccess(default=_member_access, list=None, retrieve=None),
            owner="owner",
        )
        class WrongOptionalMemos(API):
            def get_queryset(self, caller: Caller[_OtherUser] | None) -> QuerySet[Memo]:
                return Memo.all()


def test_hook_optional_caller_and_unannotated_hooks_stay_compatible() -> None:
    @crud(
        Memo,
        path="/mixed-optional",
        read=MemoOut,
        create=MemoIn,
        access=CrudAccess(default=_member_access, list=None, retrieve=None),
        owner="owner",
    )
    class MixedMemos(API):
        def get_queryset(self, caller: Caller[_MemberUser] | None) -> QuerySet[Memo]:
            if caller is None:
                return Memo.all()
            return Memo.filter(owner=caller.user.id)

    @crud(Memo, path="/unannotated", read=MemoOut, access=_unparam_access)
    class UnannotatedMemos(API):
        def get_queryset(self, principal):  # type: ignore[no-untyped-def]
            return Memo.all()

    @crud(Memo, path="/default-none", read=MemoOut)
    class DefaultNoneMemos(API):
        def get_queryset(
            self,
            caller: Caller[_MemberUser] = None,  # type: ignore[assignment]
        ) -> QuerySet[Memo]:
            return Memo.all()

    assert MixedMemos.routes()
    assert UnannotatedMemos.routes()
    assert DefaultNoneMemos.routes()


def test_from_user_extracts_attributes_and_validates_path() -> None:
    getter = from_user("org_id")
    nested = from_user("org.id")
    user = _MemberUser("ann", "acme")
    assert isinstance(getter, UserAttr)
    assert getter.attr == "org_id"
    assert getter == from_user("org_id")
    assert getter(user) == "acme"
    assert nested(user) == "acme"

    for bad in ("", " ", "123", ".org", "org.", "org..id", "org id"):
        with pytest.raises(ValueError, match="valid attribute name"):
            from_user(bad)


def test_from_user_assign_stays_on_python_lane() -> None:
    config = _CrudConfig(
        model=Memo,
        read=MemoOut,
        create=MemoIn,
        access=_member_access,
        assign={"owner": from_user("org_id")},
    )
    config.check()
    assert not assign_is_plain(config)


@pytest.mark.asyncio
async def test_from_user_and_attrgetter_assign_work_end_to_end(
    tmp_path: Path, pool: Pool
) -> None:
    class AssignedMemo(Model, table="crud_assigned_memos"):
        id: str = field(primary_key=True, default_factory=lambda: str(uuid4()))
        owner: str = field(max_length=40)
        title: str = field(max_length=40)

    class AssignedIn(Schema):
        title: str

    class AssignedOut(Schema):
        id: str
        owner: str
        title: str

    @crud(
        AssignedMemo,
        path="/from-user",
        read=AssignedOut,
        create=AssignedIn,
        access=_member_access,
        assign={"owner": from_user("org.id")},
    )
    class FromUserMemos(API):
        def get_queryset(self, caller: Caller[_MemberUser]) -> QuerySet[AssignedMemo]:
            return AssignedMemo.filter(owner=caller.user.org.id)

    @crud(
        AssignedMemo,
        path="/attrgetter",
        read=AssignedOut,
        create=AssignedIn,
        access=_member_access,
        assign={"owner": attrgetter("org_id")},
    )
    class AttrgetterMemos(API):
        pass

    db_url = f"sqlite:///{tmp_path / 'crud_assign.db'}"
    database = await Database.connect(db_url, max_size=1, wait=1)
    try:
        await database.execute(
            "CREATE TABLE crud_assigned_memos "
            "(id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL)"
        )
    finally:
        database.close()
    app = Hinoki(modules=[DatabaseModule(db_url)])
    app.include(FromUserMemos, AttrgetterMemos)
    client = await pool.start(app)
    r1 = await client.request(
        "POST",
        "/from-user",
        body(title="via-from-user"),
        content_type=JSON,
        headers=ANN,
    )
    assert r1.status == 201
    assert r1.body["owner"] == "org-ann"

    listed = await client.request("GET", "/from-user", headers=ANN)
    assert listed.status == 200
    assert [item["title"] for item in listed.body["items"]] == ["via-from-user"]

    r2 = await client.request(
        "POST",
        "/attrgetter",
        body(title="via-attrgetter"),
        content_type=JSON,
        headers=BOB,
    )
    assert r2.status == 201
    assert r2.body["owner"] == "org-bob"


def test_protected_crud_requires_explicit_access_on_handwritten_actions() -> None:
    with pytest.raises(RegistrationError, match=r"MissingAccess\.extra.*access="):

        @crud(Memo, path="/missing", read=MemoOut, access=writer)
        class MissingAccess(API):
            @http.get("/extra")
            async def extra(self) -> str:
                return "ok"

    with pytest.raises(RegistrationError, match=r"PerActionMissing\.extra.*access="):

        @crud(
            Memo,
            path="/per-action",
            read=MemoOut,
            create=MemoIn,
            access=CrudAccess(create=writer),
            owner="owner",
            identity_key=str,
        )
        class PerActionMissing(API):
            @http.get("/extra")
            async def extra(self) -> str:
                return "ok"

    with pytest.raises(RegistrationError, match=r"SocketMissing\.stream.*access="):

        @crud(Memo, path="/ws-missing", read=MemoOut, access=writer)
        class SocketMissing(API):
            @http.websocket("/ws")
            async def stream(self, socket: http.WebSocket) -> None:
                await socket.close()


async def test_protected_crud_accepts_explicit_access_and_none_opt_out() -> None:
    @crud(Memo, path="/explicit", read=MemoOut, access=writer)
    class ExplicitCrud(API):
        @http.get("/public", access=None)
        async def public_info(self) -> dict[str, str]:
            return {"scope": "public"}

        @http.get("/staff", access=writer)
        async def staff_info(self) -> dict[str, str]:
            return {"scope": "staff"}

        @http.websocket("/ws", access=None)
        async def public_ws(self, socket: http.WebSocket) -> None:
            await socket.close()

    app = Hinoki(include=[ExplicitCrud])
    async with ASGIClient(app) as client:
        public_res = await client.get("/explicit/public")
        anon_staff = await client.get("/explicit/staff")
        auth_staff = await client.get("/explicit/staff", headers=ANN)
    assert (public_res.status, public_res.body) == (200, {"scope": "public"})
    assert anon_staff.status == 401
    assert (auth_staff.status, auth_staff.body) == (200, {"scope": "staff"})


def test_unprotected_crud_and_standalone_routes_allow_omitted_access() -> None:
    @crud(Memo, path="/open", read=MemoOut)
    class OpenCrud(API):
        @http.get("/extra")
        async def extra(self) -> str:
            return "ok"

    @http.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    routes = {(r.method, r.path): r for r in Hinoki(include=[OpenCrud, health]).routes}
    assert routes[("GET", "/open/extra")].access is None
    assert routes[("GET", "/health")].access is None


def test_crud_access_on_non_generated_actions_does_not_require_handwritten_access() -> (
    None
):
    @crud(Memo, path="/no-delete", read=MemoOut, access=CrudAccess(delete=writer))
    class NoDeleteCrud(API):
        @http.get("/extra")
        async def extra(self) -> str:
            return "ok"

    @crud(
        Memo,
        path="/omit-list",
        read=MemoOut,
        access=CrudAccess(list=writer),
        omit=("list",),
    )
    class OmitListCrud(API):
        @http.get("/extra")
        async def extra(self) -> str:
            return "ok"

    routes = {
        (r.method, r.path): r
        for r in Hinoki(include=[NoDeleteCrud, OmitListCrud]).routes
    }
    assert routes[("GET", "/no-delete/extra")].access is None
    assert routes[("GET", "/omit-list/extra")].access is None


def test_protected_crud_rechecks_handwritten_access_when_decorated_after_crud() -> None:
    def add_missing_http(cls: type[API]) -> type[API]:
        @http.get("/late")
        async def late(self: API) -> str:
            return "ok"

        cls.late = late  # type: ignore[attr-defined]
        return cls

    def add_missing_ws(cls: type[API]) -> type[API]:
        @http.websocket("/late-ws")
        async def late_ws(self: API, socket: http.WebSocket) -> None:
            await socket.close()

        cls.late_ws = late_ws  # type: ignore[attr-defined]
        return cls

    def add_explicit_http(cls: type[API]) -> type[API]:
        @http.get("/late-ok", access=None)
        async def late_ok(self: API) -> str:
            return "ok"

        cls.late_ok = late_ok  # type: ignore[attr-defined]
        return cls

    @add_missing_http
    @crud(Memo, path="/late-http", read=MemoOut, access=writer)
    class LateHttp(API):
        pass

    @add_missing_ws
    @crud(Memo, path="/late-ws", read=MemoOut, access=writer)
    class LateWs(API):
        pass

    @add_explicit_http
    @crud(Memo, path="/late-ok", read=MemoOut, access=writer)
    class LateOk(API):
        pass

    with pytest.raises(RegistrationError, match=r"LateHttp\.late.*access="):
        Hinoki(include=[LateHttp])
    with pytest.raises(RegistrationError, match=r"LateWs\.late_ws.*access="):
        Hinoki(include=[LateWs])

    routes = {(r.method, r.path): r for r in Hinoki(include=[LateOk]).routes}
    assert routes[("GET", "/late-ok/late-ok")].access is None
