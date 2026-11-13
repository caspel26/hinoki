"""Sync model methods inside sync handlers, and ``asave`` dirty tracking."""

import os
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest

from hinoki import HinokiAPI, RuntimeConfig, get, post
from hinoki.blocking import run_sync
from hinoki.db import Database, DatabaseModule
from hinoki.errors import FieldError
from hinoki.orm import Model, current_database, field, use_database
from hinoki.pagination import Page, PageNumberPagination, PageParams
from hinoki.request import Request
from tests.harness import Client, Pool

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    URL is None, reason="HINOKI_TEST_DATABASE_URL is not set"
)


class Gadget(Model, table="sync_gadgets"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str
    tags: list[str] = field(default_factory=list)
    stock: int = 0


@post("/gadgets", status=201, transaction=True)
def add(request: Request, name: str) -> dict[str, Any]:
    made = Gadget.create(name=name)
    if name == "boom":
        raise RuntimeError("after the write")
    return {"id": str(made.id), "count": Gadget.all().count()}


@get("/gadgets/{gadget_id}")
def show(request: Request, gadget_id: UUID) -> dict[str, Any]:
    return {"name": Gadget.get(id=gadget_id).name}


@get("/gadgets", pagination=PageNumberPagination(page_size=2))
def listing(request: Request, page: PageParams) -> Page[dict[str, str]]:
    found = Gadget.all().order_by("name").paginate(page)
    return found.map(lambda g: {"name": g.name})


@post("/gadgets/{gadget_id}/stock")
def restock(request: Request, gadget_id: UUID) -> dict[str, Any]:
    gadget = Gadget.get(id=gadget_id)
    gadget.stock += 5
    gadget.save()
    return {
        "stock": Gadget.get(id=gadget_id).stock,
        "first": Gadget.all().first() is not None,
        "exists": Gadget.filter(name="nothing").exists(),
        "listed": len(Gadget.all().list()),
        "none": Gadget.get_or_none(id=uuid4()) is None,
        "updated": Gadget.update(id=gadget_id, name="renamed").name,
        "deleted": Gadget.delete(id=gadget_id),
    }


@get("/loop-guard")
async def blocking_on_the_loop(request: Request) -> dict[str, str]:
    Gadget.all().count()
    return {}


@pytest.fixture
async def client(pool: Pool) -> AsyncIterator[Client]:
    assert URL is not None
    admin = await Database.connect(URL, max_size=1)
    await admin.execute("DROP TABLE IF EXISTS sync_gadgets")
    await admin.execute(
        "CREATE TABLE sync_gadgets (id uuid PRIMARY KEY, name text NOT NULL, "
        "tags jsonb NOT NULL, stock int NOT NULL)"
    )
    app = HinokiAPI()
    app.include(add, show, listing, restock, blocking_on_the_loop)
    app.install(DatabaseModule(URL, max_size=3))
    yield await pool.start(app)
    await admin.execute("DROP TABLE IF EXISTS sync_gadgets")
    admin.close()


async def test_sync_handlers_use_the_models_through_the_loop(client: Client) -> None:
    created = await client.request("POST", "/gadgets", query="name=a")
    assert (created.status, created.body["count"]) == (201, 1)
    key = created.body["id"]
    assert (await client.request("GET", f"/gadgets/{key}")).body == {"name": "a"}
    changed = await client.request("POST", f"/gadgets/{key}/stock")
    assert changed.body == {
        "stock": 5,
        "first": True,
        "exists": False,
        "listed": 1,
        "none": True,
        "updated": "renamed",
        "deleted": 1,
    }


async def test_a_sync_route_with_transaction_rolls_back_on_error(
    client: Client,
) -> None:
    assert (await client.request("POST", "/gadgets", query="name=boom")).status == 500
    assert (await client.request("GET", "/gadgets")).body["total"] == 0


async def test_sync_pagination(client: Client) -> None:
    for name in ("c", "a", "b"):
        await client.request("POST", "/gadgets", query=f"name={name}")
    page = (await client.request("GET", "/gadgets", query="page=2")).body
    assert (page["total"], [i["name"] for i in page["items"]]) == (3, ["c"])


async def test_blocking_on_the_event_loop_is_refused_not_deadlocked(
    client: Client,
) -> None:
    reply = await client.request("GET", "/loop-guard")
    assert reply.status == 500


def test_sync_methods_outside_a_served_handler_are_refused() -> None:
    with pytest.raises(RuntimeError, match="only work inside sync handlers"):
        Gadget.get(id=uuid4())


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    assert URL is not None
    database = await Database.connect(URL, max_size=2)
    await database.execute("DROP TABLE IF EXISTS sync_gadgets")
    await database.execute(
        "CREATE TABLE sync_gadgets (id uuid PRIMARY KEY, name text NOT NULL, "
        "tags jsonb NOT NULL, stock int NOT NULL)"
    )
    with use_database(database):
        yield database
    await database.execute("DROP TABLE IF EXISTS sync_gadgets")
    database.close()


async def test_asave_inserts_then_updates_only_what_changed(db: Database) -> None:
    gadget = Gadget(name="g")
    assert await gadget.asave() is gadget
    gadget.stock = 3
    gadget.name = "h"
    await gadget.asave()
    stored = await Gadget.aget(id=gadget.id)
    assert (stored.name, stored.stock) == ("h", 3)

    # Another writer changes `stock`; this instance only changed `name`.
    await Gadget.aupdate(id=gadget.id, stock=99)
    gadget.name = "i"
    await gadget.asave()
    final = await Gadget.aget(id=gadget.id)
    assert (final.name, final.stock) == ("i", 99)


async def test_asave_notices_in_place_changes_and_ignores_no_ops(
    db: Database, statements: list[str]
) -> None:
    gadget = await Gadget(name="g", tags=["a"]).asave()
    loaded = await Gadget.aget(id=gadget.id)
    statements.clear()
    await loaded.asave()
    assert statements == []  # nothing changed, nothing sent
    loaded.tags.append("b")
    await loaded.asave()
    assert (await Gadget.aget(id=gadget.id)).tags == ["a", "b"]


async def test_the_primary_key_cannot_be_edited(db: Database) -> None:
    gadget = await Gadget(name="g").asave()
    gadget.id = uuid4()
    with pytest.raises(FieldError, match="primary key"):
        await gadget.asave()


@pytest.fixture
def statements(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    original = Database.fetch

    async def counting(self: Database, sql: str, *params: Any) -> Any:
        seen.append(sql)
        return await original(self, sql, *params)

    monkeypatch.setattr(Database, "fetch", counting)
    return seen


@post("/slow", transaction=True)
def slow(request: Request) -> dict[str, str]:
    run_sync(current_database().fetch("SELECT pg_sleep(4)"))
    return {}


async def test_the_deadline_cancels_work_a_sync_handler_handed_to_the_loop(
    pool: Pool,
) -> None:
    assert URL is not None
    app = HinokiAPI(runtime=RuntimeConfig(request_timeout=0.4))
    app.include(slow)
    app.install(DatabaseModule(URL, max_size=2))
    client = await pool.start(app)
    started = time.monotonic()
    reply = await client.request("POST", "/slow")
    assert reply.status == 504
    assert time.monotonic() - started < 2
