"""Regressions for the second independent review of the admin."""

import os
from collections.abc import AsyncIterator
from typing import Any, ClassVar
from urllib.parse import urlencode

import pytest

from hinoki import Access, HinokiAPI, Principal, RegistrationError
from hinoki.admin import Admin, ModelAdmin
from hinoki.db import Database, DatabaseModule
from hinoki.orm import Model, field, foreign_key
from tests.admintest import FORM, SECRET, STAFF, session, submit
from tests.harness import Client, Pool
from tests.providers import TokenAuth

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
needs_database = pytest.mark.skipif(URL is None, reason="no database")
staff = Access(auth=TokenAuth(), scopes=["posts:write"])


class Acct(Model, table="arv_accts"):
    id: int = field(primary_key=True)
    name: str = field(max_length=20)
    secret: str = ""
    tenant: int = 0


class AcctAdmin(ModelAdmin[Acct]):
    exclude = ("secret",)  # no `columns`: the default list must not show it

    def queryset(self, principal: Principal[Any]) -> Any:
        return Acct.filter(tenant=0)


class Item(Model, table="arv_items"):
    id: int = field(primary_key=True)
    label: str = field(max_length=20)
    acct_id: int | None = foreign_key(Acct, on_delete="set_null", default=None)


class Loose(Model, table="arv_loose"):
    id: int = field(primary_key=True)
    acct_id: int | None = foreign_key(Acct, on_delete="set_null", default=None)


class Parent(Model, table="arv_parents"):
    id: int = field(primary_key=True)
    name: str = field(max_length=20)


class Kid(Model, table="arv_kids"):
    id: int = field(primary_key=True)
    parent_id: int = foreign_key(Parent, on_delete="restrict")


class ParentAdmin(ModelAdmin[Parent]):
    bulk_delete = True
    columns: ClassVar[tuple[str, ...]] = ("name",)


class NoView(ModelAdmin[Acct]):
    def can(self, principal: Principal[Any], action: str, row: Any = None) -> bool:
        return action != "view"


def build(*, hide_accts: bool = False, register_accts: bool = True) -> HinokiAPI:
    admin = Admin(access=staff, secret=SECRET, site="Review")
    if register_accts:
        admin.register(Acct, NoView if hide_accts else AcctAdmin, slug="accts")
    admin.register(Item, slug="items")
    admin.register(Loose, slug="loose")
    admin.register(Parent, ParentAdmin, slug="parents")
    admin.register(Kid, slug="kids")
    app = HinokiAPI()
    app.include(admin.api())
    if URL:
        app.install(DatabaseModule(URL, max_size=3))
    return app


TABLES = ("arv_kids", "arv_parents", "arv_loose", "arv_items", "arv_accts")


@pytest.fixture
async def world(pool: Pool) -> AsyncIterator[tuple[Client, Database, Pool]]:
    assert URL is not None
    db = await Database.connect(URL, max_size=1)
    for table in TABLES:
        await db.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    await db.execute(
        "CREATE TABLE arv_accts (id bigint PRIMARY KEY, name varchar(20) NOT NULL, "
        "secret text NOT NULL, tenant bigint NOT NULL)"
    )
    await db.execute(
        "CREATE TABLE arv_items (id bigint PRIMARY KEY, label varchar(20) NOT NULL, "
        "acct_id bigint REFERENCES arv_accts(id) ON DELETE SET NULL)"
    )
    await db.execute(
        "CREATE TABLE arv_loose (id bigint PRIMARY KEY, "
        "acct_id bigint REFERENCES arv_accts(id) ON DELETE SET NULL)"
    )
    await db.execute(
        "CREATE TABLE arv_parents (id bigint PRIMARY KEY, name varchar(20) NOT NULL)"
    )
    await db.execute(
        "CREATE TABLE arv_kids (id bigint PRIMARY KEY, "
        "parent_id bigint NOT NULL REFERENCES arv_parents(id) ON DELETE RESTRICT)"
    )
    await db.execute(
        "INSERT INTO arv_accts VALUES (1, 'alice', 's3cr3t-alice', 0), (2, 'bob', 's3cr3t-bob', 1)"
    )
    yield await pool.start(build()), db, pool
    for table in TABLES:
        await db.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    db.close()


@needs_database
async def test_excluded_columns_are_never_listed_titled_or_sortable(
    world: tuple[Client, Database, Pool],
) -> None:
    client, _, _ = world
    page = await client.request("GET", "/admin/accts", headers=STAFF)
    assert "alice" in page.body and "s3cr3t" not in page.body and "bob" not in page.body
    sorted_page = await client.request(
        "GET", "/admin/accts", query="sort=secret", headers=STAFF
    )
    assert "aria-sort" not in sorted_page.body  # not a shown column: ignored
    found = await client.request("GET", "/admin/search", query="q=alice", headers=STAFF)
    assert "s3cr3t" not in found.body


@needs_database
async def test_foreign_key_selects_never_list_rows_the_caller_may_not_see(
    world: tuple[Client, Database, Pool],
) -> None:
    client, _, pool = world
    visible = await client.request("GET", "/admin/items/add", headers=STAFF)
    assert "alice" in visible.body and "bob" not in visible.body  # only tenant 0
    assert "s3cr3t" not in visible.body
    # the target has no admin at all: a plain id input, no option labels
    unregistered = await (await pool.start(build(register_accts=False))).request(
        "GET", "/admin/items/add", headers=STAFF
    )
    assert "<select" not in unregistered.body and "alice" not in unregistered.body
    # the target is registered but the caller may not view it
    hidden = await (await pool.start(build(hide_accts=True))).request(
        "GET", "/admin/items/add", headers=STAFF
    )
    assert "<select" not in hidden.body and "alice" not in hidden.body


@needs_database
async def test_the_server_refuses_a_foreign_key_outside_the_related_scope(
    world: tuple[Client, Database, Pool],
) -> None:
    client, db, _ = world
    token, headers = await session(client, "/admin/items/add")
    outside = await submit(
        client,
        "/admin/items/add",
        {"id": "1", "label": "x", "acct_id": "2", "csrf": token},
        headers,
    )
    assert outside.status == 422 and "not available" in outside.body
    inside = await submit(
        client,
        "/admin/items/add",
        {"id": "1", "label": "x", "acct_id": "1", "csrf": token},
        headers,
    )
    assert inside.status == 303
    assert (await db.fetch("SELECT acct_id FROM arv_items"))[0]["acct_id"] == 1


@needs_database
@pytest.mark.parametrize(
    "path",
    [
        "/admin/accts/a%00b",
        "/admin/accts/99999999999999999999999",
        "/admin/accts/abc",
        "/admin/accts/1/delete",
        "/admin/parents/a%00b/delete",
    ],
)
async def test_unusable_keys_are_404_not_500(
    world: tuple[Client, Database, Pool], path: str
) -> None:
    client, _, _ = world
    reply = await client.request("GET", path, headers=STAFF)
    assert reply.status in {200, 404}  # /accts/1/delete is a real, allowed page


@needs_database
async def test_nul_in_search_text_is_dropped_not_a_server_error(
    world: tuple[Client, Database, Pool],
) -> None:
    client, _, _ = world
    for path in ("/admin/accts", "/admin/search"):
        assert (
            await client.request("GET", path, query="q=a%00b", headers=STAFF)
        ).status == 200


@needs_database
async def test_bulk_delete_is_all_or_nothing(
    world: tuple[Client, Database, Pool],
) -> None:
    client, db, _ = world
    await db.execute("INSERT INTO arv_parents VALUES (1, 'a'), (2, 'b'), (3, 'c')")
    await db.execute("INSERT INTO arv_kids VALUES (1, 3)")  # parent 3 cannot be deleted
    token, headers = await session(client, "/admin/parents")
    body = urlencode(
        [("csrf", token), ("confirmed", "1"), ("ids", "1"), ("ids", "2"), ("ids", "3")]
    ).encode()
    reply = await client.request(
        "POST", "/admin/parents/bulk", body, content_type=FORM, headers=headers
    )
    assert reply.status == 409
    assert len(await db.fetch("SELECT id FROM arv_parents")) == 3  # nothing was deleted


@needs_database
async def test_error_answers_under_the_admin_carry_the_security_headers(
    world: tuple[Client, Database, Pool],
) -> None:
    client, _, _ = world
    denied = await client.request("GET", "/admin/accts")  # no credentials: 401 JSON
    assert denied.status == 401
    assert denied.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in denied.headers["content-security-policy"]
    assert denied.headers["cache-control"] == "no-store"
    elsewhere = await client.request("GET", "/nothing")
    assert "content-security-policy" not in elsewhere.headers


def test_a_docs_page_may_not_shadow_a_route() -> None:
    admin = Admin(access=staff, secret=SECRET)
    admin.register(Parent, slug="parents")
    other = HinokiAPI(openapi_url="/openapi.json", docs_url="/admin")
    other.include(admin.api())  # the admin's index answers GET /admin
    with pytest.raises(RegistrationError, match="documentation viewer"):
        other.asgi()
