"""Sync and async handlers served together under concurrency: nothing hangs,
every answer is a success or the documented 503, and the worker stays usable."""

import asyncio
import os
import time
from collections import Counter
from collections.abc import AsyncIterator

import pytest

from hinoki import Hinoki, Request, http
from hinoki.db import Database, DatabaseModule
from hinoki.orm import Model, field, use_database
from hinoki.runtime.config import RuntimeConfig
from hinoki.testing import ASGIClient

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(URL is None, reason="no database")


class Counter2(Model, table="ml_rows"):
    id: int = field(primary_key=True)
    n: int = 0


@http.get("/sync")
def sync_one(request: Request) -> dict[str, int]:
    time.sleep(0.01)  # blocking work, allowed in a sync handler
    return {"n": Counter2.get(id=1).n}


@http.get("/async")
async def async_one(request: Request) -> dict[str, int]:
    await asyncio.sleep(0.01)
    return {"n": (await Counter2.aget(id=1)).n}


@http.get("/slow")
def slow(request: Request) -> dict[str, str]:
    time.sleep(1.5)
    return {"done": "yes"}


@pytest.fixture
async def table() -> AsyncIterator[None]:
    assert URL is not None
    database = await Database.connect(URL, max_size=2)
    await database.execute("DROP TABLE IF EXISTS ml_rows")
    await database.execute("CREATE TABLE ml_rows (id int primary key, n int not null)")
    with use_database(database):
        await Counter2.acreate(id=1, n=7)
        yield
    await database.execute("DROP TABLE IF EXISTS ml_rows")
    database.close()


def build(workers: int, queue: int, timeout: float = 30.0) -> Hinoki:
    assert URL is not None
    app = Hinoki(
        runtime=RuntimeConfig(
            sync_workers=workers, sync_queue=queue, request_timeout=timeout
        )
    )
    app.include(sync_one, async_one, slow)
    app.install(DatabaseModule(URL, max_size=3))
    return app


async def test_mixed_traffic_completes_and_overload_is_a_clean_503(table: None) -> None:
    async with ASGIClient(build(workers=2, queue=4)) as client:
        paths = ["/sync", "/async"] * 60
        replies = await asyncio.wait_for(
            asyncio.gather(*(client.get(p) for p in paths)), timeout=60
        )
        seen = Counter(r.status for r in replies)
        assert set(seen) <= {200, 503}, seen
        assert seen[200] >= 60  # every async request and some sync ones
        assert all(r.body == {"n": 7} for r in replies if r.status == 200)
        assert all(r.body["error"] == "overloaded" for r in replies if r.status == 503)
        # the worker recovered: a later request is served normally
        assert (await client.get("/sync")).status == 200


async def test_a_deadline_on_a_slow_sync_handler_frees_the_worker(table: None) -> None:
    async with ASGIClient(build(workers=2, queue=2, timeout=0.3)) as client:
        started = time.monotonic()
        late = await client.get("/slow")
        assert late.status == 504 and time.monotonic() - started < 1.2
        assert (await client.get("/async")).status == 200
