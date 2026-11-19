"""The native lane serves provable ``async def`` handlers without an asyncio
task. The asyncio path (``native_dispatch=False``) is the oracle: both must
answer every request alike."""

import asyncio
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, ClassVar

import pytest

from hinoki import Hinoki, Module, RuntimeConfig, http
from hinoki.context import request_id
from hinoki.db import Database, DatabaseModule
from hinoki.modules import ModuleContext
from hinoki.orm import Model, field
from hinoki.orm.options import table
from hinoki.routescli import lanes_of
from hinoki.runtime.kiso import KisoWorker
from hinoki.runtime.nativelane import lane_refusal, resume
from tests.kisoservertest import Received, fetch

POSTGRES = os.environ.get("HINOKI_TEST_DATABASE_URL")


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgres",
            marks=pytest.mark.skipif(
                POSTGRES is None, reason="HINOKI_TEST_DATABASE_URL is not set"
            ),
        ),
    ]
)
def url(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    if request.param == "postgres":
        assert POSTGRES is not None
        return POSTGRES
    return f"sqlite://{tmp_path / 'lane.sqlite3'}"


@table("lane_notes")
class LaneNote(Model):
    id: int = field(primary_key=True)
    title: str


class Holder(Module):
    name = "lane-holder"
    requires: ClassVar[tuple[type[Module], ...]] = (DatabaseModule,)
    current: ClassVar[list[Database]] = []

    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        database = ctx.get(Database)
        await database.execute(
            "create table if not exists lane_notes "
            "(id integer primary key, title text not null)"
        )
        await database.execute("delete from lane_notes")
        await database.execute(
            "insert into lane_notes (id, title) "
            "values (1, 'one'), (2, 'two'), (3, 'three')"
        )
        self.current.append(database)
        yield


@http.get("/lane/note/{id}")
async def note(id: int) -> dict[str, Any]:
    found = await LaneNote.aget(id=id)
    return {"id": found.id, "title": found.title}


@http.get("/lane/notes")
async def notes(limit: int = 2) -> list[dict[str, Any]]:
    rows = await LaneNote.all().order_by("id").limit(limit)
    return [{"id": row.id, "title": row.title} for row in rows]


@http.get("/lane/count")
async def count() -> dict[str, int]:
    return {"count": await LaneNote.all().acount()}


@http.get("/lane/pure")
async def pure() -> dict[str, str]:
    return {"hello": "lane"}


@http.get("/lane/context")
async def seen_context() -> dict[str, str]:
    return {"request_id": request_id.get() or ""}


@http.get("/lane/boom")
async def boom() -> dict[str, str]:
    raise ValueError("boom")


@http.get("/lane/not-found")
async def missing() -> dict[str, str]:
    return {"title": (await LaneNote.aget(id=404)).title}


@http.get("/lane/sleep")
async def sleeping() -> dict[str, str]:
    await asyncio.sleep(0.01)
    return {"slept": "yes"}


@http.get("/lane/sync")
def plain() -> dict[str, str]:
    return {"plain": "sync"}


HANDLERS = (note, notes, count, pure, seen_context, boom, missing, sleeping, plain)
SERVED = {"/lane/note/{id}", "/lane/notes", "/lane/count", "/lane/pure"}
SERVED |= {"/lane/context", "/lane/boom", "/lane/not-found"}


def make_app(url: str, **runtime: float) -> Hinoki:
    app = Hinoki(runtime=RuntimeConfig(**runtime))  # type: ignore[arg-type]
    app.include(*HANDLERS)
    app.install(DatabaseModule(url), Holder())
    return app


def normalized(received: Received) -> tuple[int, list[tuple[str, str]], bytes]:
    """Without the request id, the one value that differs per request."""
    pattern = re.compile(rb"[0-9a-f]{32}")
    headers = [(k, v) for k, v in received.headers if k not in {"x-request-id", "date"}]
    return received.status, headers, pattern.sub(b"<id>", received.body)


async def serve_all(
    url: str, targets: list[str], *, native: bool
) -> tuple[list[Received], list[int]]:
    worker = KisoWorker(make_app(url), "127.0.0.1", 0, native_dispatch=native)
    port = await worker.start()
    started: list[int] = []
    if worker.lane is not None:
        begin = worker.lane.start

        def recorded(request: Any) -> bool:
            taken = begin(request)
            started.append(int(taken))
            return taken

        worker.lane.start = recorded  # type: ignore[method-assign]
    try:
        return [await fetch(port, "GET", target) for target in targets], started
    finally:
        await worker.stop()


TARGETS = [
    "/lane/note/1",
    "/lane/note/3",
    "/lane/note/abc",
    "/lane/notes",
    "/lane/notes?limit=3",
    "/lane/notes?limit=x",
    "/lane/count",
    "/lane/pure",
    "/lane/context",
    "/lane/boom",
    "/lane/not-found",
    "/lane/sleep",
    "/lane/sync",
    "/lane/nowhere",
]


async def test_the_lane_answers_like_the_asyncio_path(url: str) -> None:
    oracle, none = await serve_all(url, TARGETS, native=False)
    served, started = await serve_all(url, TARGETS, native=True)
    assert none == []
    assert [normalized(r) for r in served] == [normalized(r) for r in oracle]
    assert served[0].json() == {"id": 1, "title": "one"}
    assert served[9].status == 500
    assert served[10].status == 404
    # note x2, notes x2, count, pure, context, boom, not-found take the lane;
    # the two input errors, the sleeping and the sync handler do not; the
    # unknown path never leaves Rust.
    assert started.count(1) == 9
    assert started.count(0) == 4


async def test_a_driven_handler_runs_in_its_own_request_context(
    url: str,
) -> None:
    worker = KisoWorker(make_app(url), "127.0.0.1", 0)
    port = await worker.start()
    try:
        first, second = await asyncio.gather(
            fetch(port, "GET", "/lane/context"), fetch(port, "GET", "/lane/context")
        )
    finally:
        await worker.stop()
    ids = {first.json()["request_id"], second.json()["request_id"]}
    assert len(ids) == 2
    assert first.header("x-request-id") == first.json()["request_id"]


async def test_lane_refusal_names_why_a_route_stays_on_asyncio(
    url: str,
) -> None:
    app = make_app(url)
    reasons = {b.route.path: lane_refusal(b) for b in app.bindings}
    assert {path for path, why in reasons.items() if why is None} == SERVED
    assert reasons["/lane/sync"] == "not an async def handler"
    assert reasons["/lane/sleep"] is not None


async def test_the_switch_keeps_every_handler_on_asyncio(url: str) -> None:
    worker = KisoWorker(make_app(url), "127.0.0.1", 0, native_dispatch=False)
    await worker.start()
    try:
        assert worker.lane is None
    finally:
        await worker.stop()


async def test_routes_report_the_native_lane(url: str) -> None:
    app = make_app(url)
    lanes = {r.path: lanes_of(app)[id(r)] for r in app.routes}
    assert lanes["/lane/note/{id}"] == ("native", "native_dispatch")
    assert lanes["/lane/sleep"][0] == "python"


async def test_resume_continues_a_coroutine_in_a_task() -> None:
    async def work() -> int:
        await asyncio.sleep(0.01)
        return 5

    coro = work()
    yielded = coro.send(None)
    assert await resume(coro, yielded) == 5


async def test_cancelling_a_resumed_coroutine_runs_its_cleanup() -> None:
    cleaned = asyncio.Event()

    async def work() -> None:
        try:
            await asyncio.sleep(10)
        finally:
            cleaned.set()

    coro = work()
    task = asyncio.ensure_future(resume(coro, coro.send(None)))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
