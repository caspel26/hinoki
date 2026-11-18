"""transaction=True on the native handler lane against a disposable PostgreSQL
(skipped without one)."""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import ClassVar

import pytest

from hinoki import Hinoki, Module, RuntimeConfig, Schema, http
from hinoki.db import Database, DatabaseModule, UniqueViolation
from hinoki.errors import Conflict
from hinoki.modules import ModuleContext
from hinoki.runtime.rustserver import RustWorker
from tests.rustservertest import fetch, record_lanes

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    URL is None, reason="HINOKI_TEST_DATABASE_URL is not set"
)
TABLE = "native_tx_items"
JSON = "content-type: application/json\r\n"
INSERT = f"INSERT INTO {TABLE} (name) VALUES ($1)"  # noqa: S608


class Item(Schema):
    name: str


class Holder(Module):
    name = "native-tx-holder"
    requires = (DatabaseModule,)
    current: ClassVar[list[Database]] = []

    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        self.current.append(ctx.get(Database))
        yield


def database() -> Database:
    return Holder.current[-1]


@http.post("/ok", status=201, transaction=True)
async def create_ok(data: Item) -> dict[str, str]:
    await database().execute(INSERT, data.name)
    return {"name": data.name}


@http.post("/crash", transaction=True)
async def create_then_crash(data: Item) -> dict[str, str]:
    await database().execute(INSERT, data.name)
    raise RuntimeError("secret failure")


@http.post("/conflict", transaction=True)
async def create_then_conflict(data: Item) -> dict[str, str]:
    await database().execute(INSERT, data.name)
    raise Conflict("Already there.", code="already_there")


@http.post("/swallow", transaction=True)
async def swallow(data: Item) -> dict[str, str]:
    """Catches a constraint error and returns normally: the commit must fail."""
    await database().execute(INSERT, data.name)
    with suppress(UniqueViolation):
        await database().execute(INSERT, data.name)
    return {"name": data.name}


@http.post("/slow", transaction=True)
async def slow(data: Item) -> dict[str, str]:
    await database().execute(INSERT, data.name)
    await database().fetch("SELECT pg_sleep(5)")
    return {}


@http.delete("/clear/{name}", transaction=True)
def clear(name: str) -> dict[str, str]:
    return {"name": name}


def make_app(**runtime: float) -> Hinoki:
    assert URL is not None
    app = Hinoki(runtime=RuntimeConfig(**runtime))  # type: ignore[arg-type]
    app.include(create_ok, create_then_crash, create_then_conflict, swallow, slow)
    app.include(clear)
    app.install(DatabaseModule(URL, max_size=3, wait=0.5), Holder())
    return app


@pytest.fixture(autouse=True)
async def table() -> AsyncIterator[Database]:
    assert URL is not None
    admin = await Database.connect(URL, max_size=1)
    await admin.execute(f"DROP TABLE IF EXISTS {TABLE}")
    await admin.execute(f"CREATE TABLE {TABLE} (name text PRIMARY KEY)")
    Holder.current.clear()
    yield admin
    await admin.execute(f"DROP TABLE IF EXISTS {TABLE}")
    admin.close()


async def post(port: int, path: str) -> tuple[int, dict[str, object]]:
    body = b'{"name": "a"}'
    extra = f"{JSON}content-length: {len(body)}\r\n"
    received = await fetch(port, "POST", path, extra, body=body)
    return received.status, received.json()


async def count(admin: Database) -> int:
    rows = await admin.fetch(f"SELECT count(*)::int AS c FROM {TABLE}")  # noqa: S608
    return int(rows[0]["c"])


@pytest.mark.parametrize(
    ("path", "status", "rows"),
    [
        ("/ok", 201, 1),
        ("/crash", 500, 0),
        ("/conflict", 409, 0),
        ("/swallow", 500, 0),
    ],
)
async def test_native_transaction_commits_or_rolls_back_like_the_pipeline(
    table: Database, path: str, status: int, rows: int
) -> None:
    worker = RustWorker(make_app(), "127.0.0.1", 0)
    port = await worker.start()
    try:
        lanes = record_lanes(worker)
        reply, body = await post(port, path)
        assert lanes == [True]
        assert reply == status
        assert "secret" not in str(body)
        assert await count(table) == rows
    finally:
        await worker.stop()


async def test_native_transaction_timeout_rolls_back_and_keeps_serving(
    table: Database,
) -> None:
    worker = RustWorker(make_app(request_timeout=0.3), "127.0.0.1", 0)
    port = await worker.start()
    try:
        assert (await post(port, "/slow"))[0] == 504
        await asyncio.sleep(0.1)
        assert await count(table) == 0
        assert (await post(port, "/ok"))[0] == 201
    finally:
        await worker.stop()


async def test_a_sync_delete_with_a_transaction_is_native() -> None:
    worker = RustWorker(make_app(), "127.0.0.1", 0)
    port = await worker.start()
    try:
        lanes = record_lanes(worker)
        received = await fetch(port, "DELETE", "/clear/x")
        assert lanes == [True]
        assert received.status == 200
    finally:
        await worker.stop()
