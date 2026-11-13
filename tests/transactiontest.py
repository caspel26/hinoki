"""transaction=True end to end against a disposable PostgreSQL (skipped without one)."""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import ClassVar

import pytest

from hinoki import Conflict, HinokiAPI, Module, RuntimeConfig, Schema, get, post
from hinoki.db import Database, DatabaseModule, UniqueViolation
from hinoki.modules import ModuleContext
from tests.harness import Client, Pool

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    URL is None, reason="HINOKI_TEST_DATABASE_URL is not set"
)
TABLE = "tx_items"
JSON = "application/json"


class Item(Schema):
    name: str


class Holder(Module):
    """Remembers the worker's Database so module-level handlers can use it."""

    name = "holder"
    requires = (DatabaseModule,)
    current: ClassVar[list[Database]] = []

    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        self.current.append(ctx.get(Database))
        yield


def database() -> Database:
    return Holder.current[-1]


@post("/ok", status=201, transaction=True)
async def create_ok(data: Item) -> dict[str, str]:
    await database().execute(f"INSERT INTO {TABLE} (name) VALUES ($1)", data.name)  # noqa: S608
    return {"name": data.name}


@post("/crash", transaction=True)
async def create_then_crash(data: Item) -> dict[str, str]:
    await database().execute(f"INSERT INTO {TABLE} (name) VALUES ($1)", data.name)  # noqa: S608
    raise RuntimeError("secret failure")


@post("/conflict", transaction=True)
async def create_then_conflict(data: Item) -> dict[str, str]:
    await database().execute(f"INSERT INTO {TABLE} (name) VALUES ($1)", data.name)  # noqa: S608
    raise Conflict("Already there.", code="already_there")


@post("/duplicate", transaction=True)
async def create_duplicate(data: Item) -> dict[str, str]:
    sql = f"INSERT INTO {TABLE} (name) VALUES ($1)"  # noqa: S608
    await database().execute(sql, data.name)
    await database().execute(sql, data.name)
    return {}


@post("/slow", transaction=True)
async def create_slow(data: Item) -> dict[str, str]:
    await database().execute(f"INSERT INTO {TABLE} (name) VALUES ($1)", data.name)  # noqa: S608
    await database().fetch("SELECT pg_sleep(5)")
    return {}


@get("/count")
async def count() -> dict[str, int]:
    rows = await database().fetch(f"SELECT count(*)::int AS c FROM {TABLE}")  # noqa: S608
    return {"count": rows[0]["c"]}


@post("/no-db", transaction=True)
async def needs_database() -> None: ...


def make_app(**runtime: float) -> HinokiAPI:
    assert URL is not None
    app = HinokiAPI(runtime=RuntimeConfig(**runtime))  # type: ignore[arg-type]
    app.include(create_ok, create_then_crash, create_then_conflict, create_duplicate)
    app.include(create_slow, count)
    app.install(DatabaseModule(URL, max_size=3, wait=0.5), Holder())
    return app


@pytest.fixture(autouse=True)
async def table() -> AsyncIterator[None]:
    assert URL is not None
    admin = await Database.connect(URL, max_size=1)
    await admin.execute(f"DROP TABLE IF EXISTS {TABLE}")
    await admin.execute(f"CREATE TABLE {TABLE} (name text PRIMARY KEY)")
    Holder.current.clear()
    yield
    await admin.execute(f"DROP TABLE IF EXISTS {TABLE}")
    admin.close()


async def rows(pool: Pool) -> int:
    client = pool.clients[0]
    return int((await client.request("GET", "/count")).body["count"])


async def test_a_successful_action_commits(pool: Pool) -> None:
    client = await pool.start(make_app())
    reply = await client.request("POST", "/ok", b'{"name": "a"}', content_type=JSON)
    assert reply.status == 201
    assert await rows(pool) == 1


async def test_an_unexpected_error_rolls_back_and_does_not_leak(pool: Pool) -> None:
    client = await pool.start(make_app())
    reply = await client.request("POST", "/crash", b'{"name": "a"}', content_type=JSON)
    assert reply.status == 500
    assert "secret" not in str(reply.body)
    assert await rows(pool) == 0


async def test_a_public_error_still_rolls_back_before_translation(pool: Pool) -> None:
    client = await pool.start(make_app())
    reply = await client.request(
        "POST", "/conflict", b'{"name": "a"}', content_type=JSON
    )
    assert (reply.status, reply.body["error"]) == (409, "already_there")
    assert await rows(pool) == 0


async def test_a_database_constraint_error_rolls_back_the_whole_action(
    pool: Pool,
) -> None:
    app = make_app()
    app.errors.handle(UniqueViolation)(unique_to_conflict)
    client = await pool.start(app)
    reply = await client.request(
        "POST", "/duplicate", b'{"name": "a"}', content_type=JSON
    )
    assert (reply.status, reply.body["error"]) == (409, "unique_conflict")
    assert await rows(pool) == 0


def unique_to_conflict(error: UniqueViolation) -> Conflict:
    return Conflict("A unique value is already in use.", code="unique_conflict")


async def test_a_request_timeout_rolls_back_and_the_worker_keeps_serving(
    pool: Pool,
) -> None:
    client = await pool.start(make_app(request_timeout=0.3))
    reply = await client.request("POST", "/slow", b'{"name": "a"}', content_type=JSON)
    assert reply.status == 504
    await asyncio.sleep(0.1)
    assert await rows(pool) == 0
    assert (await client.request("GET", "/count")).status == 200


async def test_transaction_without_a_database_fails_startup() -> None:
    app = HinokiAPI()
    app.include(needs_database)
    client = Client(app.asgi())
    assert await client.lifespan_event("startup") == "lifespan.startup.failed"
