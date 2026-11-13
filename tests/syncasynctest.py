"""The same scenario through the sync and the async surfaces gives the same results."""

import asyncio
import os
import time
from collections.abc import AsyncIterator

import pytest

from hinoki import Access, BearerAuth, Caller, Hinoki, Request, Schema, get, post
from hinoki.db import Database, DatabaseModule
from hinoki.orm import Model, field, use_database
from hinoki.runtime.config import RuntimeConfig
from hinoki.testing import ASGIClient, SyncClient

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
needs_database = pytest.mark.skipif(URL is None, reason="no database")
DROP = "DROP TABLE IF EXISTS sa_items"


class Item(Model, table="sa_items"):
    id: int = field(primary_key=True)
    name: str = field(max_length=40)
    hits: int = 0


class ItemOut(Schema):
    id: int
    name: str
    hits: int


class Tokens(BearerAuth[str]):
    """Written with plain ``def``: runs on the sync executor."""

    def resolve_token(self, token: str) -> Caller[str] | None:
        item = Item.get_or_none(name=token)  # a sync model call inside a sync hook
        return Caller(id=token, user=token, permissions=frozenset()) if item else None


guard = Access(auth=Tokens())


@get("/sync/{item_id}", access=guard)
def read_sync(request: Request, item_id: int) -> ItemOut:
    return ItemOut.from_model(Item.get(id=item_id))


@get("/async/{item_id}", access=guard)
async def read_async(request: Request, item_id: int) -> ItemOut:
    return ItemOut.from_model(await Item.aget(id=item_id))


@post("/sync/{item_id}/hit", transaction=True)
def hit_sync(request: Request, item_id: int) -> ItemOut:
    (row,) = Item.update_where({"id": item_id}, {"hits": 5})
    Item.filter(id=item_id).update({"hits": row.hits + 1})
    return ItemOut.from_model(Item.get(id=item_id))


@post("/async/{item_id}/hit", transaction=True)
async def hit_async(request: Request, item_id: int) -> ItemOut:
    (row,) = await Item.aupdate_where({"id": item_id}, {"hits": 5})
    await Item.filter(id=item_id).aupdate({"hits": row.hits + 1})
    return ItemOut.from_model(await Item.aget(id=item_id))


def build() -> Hinoki:
    assert URL is not None
    app = Hinoki()
    app.include(read_sync, read_async, hit_sync, hit_async)
    app.install(DatabaseModule(URL, max_size=3))
    return app


@pytest.fixture
async def seeded() -> AsyncIterator[None]:
    assert URL is not None
    database = await Database.connect(URL, max_size=2)
    await database.execute(DROP)
    await database.execute(
        "CREATE TABLE sa_items (id int primary key, name varchar(40) not null, "
        "hits int not null default 0)"
    )
    with use_database(database):
        await Item.acreate(id=1, name="tok")
        yield
    await database.execute(DROP)
    database.close()


AUTH = {"authorization": "Bearer tok"}
TOKEN = "t"  # noqa: S105 - a fixture token, not a secret


@needs_database
async def test_async_client_sync_and_async_handlers_agree(seeded: None) -> None:
    async with ASGIClient(build()) as client:
        one = await client.get("/sync/1", headers=AUTH)
        two = await client.get("/async/1", headers=AUTH)
        assert one.status == two.status == 200 and one.body == two.body
        assert (
            await client.get("/sync/1", headers={"authorization": "Bearer x"})
        ).status == 401
        assert (await client.post("/sync/1/hit")).body["hits"] == 6
        assert (await client.post("/async/1/hit")).body["hits"] == 6


@needs_database
def test_sync_client_drives_the_same_app_without_async(seeded: None) -> None:
    # `seeded` is an async fixture: this test only needs the table it created.
    with SyncClient(build()) as client:
        assert client.get("/sync/1", headers=AUTH).body["name"] == "tok"
        assert client.get("/async/1", headers=AUTH).status == 200
        assert client.post("/sync/1/hit").body["hits"] == 6
        assert client.get("/missing").status == 404


class SlowTokens(BearerAuth[str]):
    """A plain-def hook that holds an executor slot for a while."""

    def resolve_token(self, token: str) -> Caller[str] | None:
        time.sleep(0.3)
        return Caller(id=token, user=token, permissions=frozenset())


@get("/slow-auth", access=Access(auth=SlowTokens()))
async def slow_auth(request: Request) -> dict[str, bool]:
    return {"ok": True}


async def test_a_saturated_pool_answers_503_even_inside_an_auth_hook() -> None:
    app = Hinoki(runtime=RuntimeConfig(sync_workers=1, sync_queue=0))
    app.include(slow_auth)
    async with ASGIClient(app) as client:
        replies = await asyncio.gather(
            *(client.get("/slow-auth", token=TOKEN) for _ in range(3))
        )
    codes = sorted(r.status for r in replies)
    assert codes == [200, 503, 503]
    assert all(
        r.body["error"] == "overloaded" and "retry-after" in r.headers
        for r in replies
        if r.status == 503
    )


def test_sync_client_refuses_misuse_instead_of_hanging() -> None:
    app = Hinoki()
    app.include(slow_auth)
    client = SyncClient(app)
    with pytest.raises(RuntimeError, match="with` block"):
        client.get("/slow-auth")
    client.__exit__(None, None, None)  # exit without enter is a no-op
    with client:
        pass
    with pytest.raises(RuntimeError, match="with` block"):
        client.get("/slow-auth")
    client.__exit__(None, None, None)  # a second exit is a no-op
    with pytest.raises(RuntimeError, match="once"):
        client.__enter__()
