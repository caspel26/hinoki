"""The native lane serves provable ``async def`` handlers without an asyncio
task. The asyncio path (``native_dispatch=False``) is the oracle: both must
answer every request alike."""

import asyncio
import os
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, ClassVar

import pytest

from hinoki import (
    Access,
    Caller,
    Hinoki,
    HTMLRenderer,
    JSONRenderer,
    Module,
    Query,
    RateLimitContext,
    RuntimeConfig,
    Schema,
    http,
)
from hinoki.context import request_id
from hinoki.db import Database, DatabaseModule
from hinoki.errors import TooManyRequests
from hinoki.modules import ModuleContext
from hinoki.orm import Model, field
from hinoki.orm.options import table
from hinoki.routescli import lanes_of
from hinoki.runtime.kiso import KisoWorker
from hinoki.runtime.nativelane import lane_refusal, resume
from tests.kisoservertest import Received, fetch
from tests.providers import TokenAuth

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


class SlowTokens(TokenAuth):
    """A provider that waits on asyncio, so the guard hands the coroutine over."""

    async def resolve_token(self, token: str) -> Caller[str] | None:
        await asyncio.sleep(0.001)
        return await super().resolve_token(token)


class RefuseAll:
    async def check(self, context: RateLimitContext) -> None:
        raise TooManyRequests(7)


class RefuseSync:
    def check(self, context: RateLimitContext) -> None:
        raise TooManyRequests(3)


class Search(Schema):
    below: int = 10


STAFF = Access(auth=TokenAuth(), permissions=["posts:write"])


@http.get("/lane/guarded/{id}", access=STAFF)
async def guarded(id: int, who: Caller[str]) -> dict[str, Any]:
    found = await LaneNote.aget(id=id)
    return {"id": found.id, "title": found.title, "who": who.id}


@http.get("/lane/slow-auth", access=Access(auth=SlowTokens()))
async def slow_auth(who: Caller[str]) -> dict[str, str]:
    return {"who": who.id}


@http.get("/lane/limited", rate_limit=RefuseAll())
async def limited() -> dict[str, str]:
    return {"ran": "yes"}


@http.get("/lane/limited-sync", rate_limit=RefuseSync())
async def limited_sync() -> dict[str, str]:
    return {"ran": "yes"}


@http.get("/lane/search")
async def search(filters: Query[Search]) -> dict[str, int]:
    rows = await LaneNote.all().order_by("id").limit(filters.below)
    return {"count": len(rows)}


@http.get("/lane/negotiated", renderers=[JSONRenderer(), HTMLRenderer()])
async def negotiated() -> str:
    return "<p>hello</p>"


class NewNote(Schema):
    title: str
    rank: int = 1


@http.post("/lane/echo", status=201)
async def echo(data: NewNote) -> dict[str, Any]:
    return {
        "title": data.title,
        "rank": data.rank,
        "total": await LaneNote.all().acount(),
    }


@http.put("/lane/note/{id}")
async def replace(id: int, data: NewNote) -> dict[str, Any]:
    found = await LaneNote.aget(id=id)
    return {"id": found.id, "was": found.title, "now": data.title}


@http.patch("/lane/guarded/{id}", access=STAFF)
async def amend(id: int, data: NewNote, who: Caller[str]) -> dict[str, Any]:
    return {"id": id, "title": data.title, "who": who.id}


@http.query("/lane/find")
async def find(data: NewNote) -> dict[str, Any]:
    return {"title": data.title, "total": await LaneNote.all().acount()}


@http.post("/lane/limited-body", status=201, rate_limit=RefuseAll())
async def limited_body(data: NewNote) -> dict[str, str]:
    return {"title": data.title}


HANDLERS = (note, notes, count, pure, seen_context, boom, missing, sleeping, plain)
GUARDED = (guarded, slow_auth, limited, limited_sync, search, negotiated)
BODIES = (echo, replace, amend, find, limited_body)
SERVED = {"/lane/note/{id}", "/lane/notes", "/lane/count", "/lane/pure"}
SERVED |= {"/lane/context", "/lane/boom", "/lane/not-found"}
SERVED |= {"/lane/guarded/{id}", "/lane/slow-auth", "/lane/limited"}
SERVED |= {"/lane/limited-sync", "/lane/search", "/lane/negotiated"}
SERVED |= {"/lane/echo", "/lane/limited-body", "/lane/find"}


def make_app(url: str, **runtime: float) -> Hinoki:
    app = Hinoki(runtime=RuntimeConfig(**runtime))  # type: ignore[arg-type]
    app.include(*HANDLERS, *GUARDED, *BODIES)
    app.install(DatabaseModule(url), Holder())
    return app


def normalized(received: Received) -> tuple[int, list[tuple[str, str]], bytes]:
    """Without the request id, the one value that differs per request."""
    pattern = re.compile(rb"[0-9a-f]{32}")
    headers = [(k, v) for k, v in received.headers if k not in {"x-request-id", "date"}]
    return received.status, headers, pattern.sub(b"<id>", received.body)


Sent = str | tuple[str, str] | tuple[str, str, str, bytes]


def request_of(sent: Sent) -> tuple[str, str, str, bytes]:
    """The method, target, extra headers and body of a request to send."""
    if isinstance(sent, str):
        return "GET", sent, "", b""
    if len(sent) == 2:
        return "GET", sent[0], sent[1], b""
    method, target, extra, body = sent
    return method, target, f"{extra}content-length: {len(body)}\r\n", body


async def serve_all(
    url: str, targets: Sequence[Sent], *, native: bool
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
        sent = [request_of(target) for target in targets]
        return [
            await fetch(port, method, target, extra, body=body)
            for method, target, extra, body in sent
        ], started
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


def bearer(token: str) -> str:
    return f"authorization: Bearer {token}\r\n"


GUARDED_TARGETS: list[Sent] = [
    ("/lane/guarded/1", bearer("editor-token")),
    ("/lane/guarded/9", bearer("editor-token")),
    ("/lane/guarded/1", bearer("reader-token")),
    ("/lane/guarded/1", bearer("nobody")),
    ("/lane/guarded/1", ""),
    ("/lane/slow-auth", bearer("editor-token")),
    ("/lane/slow-auth", ""),
    "/lane/limited",
    "/lane/limited-sync",
    "/lane/search",
    "/lane/search?below=2",
    "/lane/search?below=x",
    "/lane/search?other=1",
    "/lane/negotiated",
    ("/lane/negotiated", "accept: text/html\r\n"),
    ("/lane/negotiated", "accept: image/png\r\n"),
]


async def test_guarded_routes_take_the_lane_like_the_asyncio_path(url: str) -> None:
    oracle, none = await serve_all(url, GUARDED_TARGETS, native=False)
    served, started = await serve_all(url, GUARDED_TARGETS, native=True)
    assert none == []
    assert [normalized(r) for r in served] == [normalized(r) for r in oracle]
    assert [r.status for r in served] == [
        200, 404, 403, 401, 401, 200, 401, 429, 429, 200, 200, 422, 422,
        200, 200, 406,
    ]  # fmt: skip
    # Only the unacceptable request stays off the lane: Rust refuses it. The
    # guards judge credentials and query values inside the lane.
    assert started.count(0) == 1
    assert started.count(1) == 15


async def test_a_guard_waiting_on_asyncio_does_not_run_the_handler_twice(
    url: str,
) -> None:
    served, _ = await serve_all(
        url, [("/lane/slow-auth", bearer("editor-token"))] * 5, native=True
    )
    assert [r.json() for r in served] == [{"who": "u-editor"}] * 5


JSON = "content-type: application/json\r\n"
BODY_TARGETS: list[Sent] = [
    ("POST", "/lane/echo", JSON, b'{"title":"a"}'),
    ("POST", "/lane/echo", JSON, b'{"title":"a","rank":4}'),
    ("POST", "/lane/echo", JSON, b'{"title":5}'),
    ("POST", "/lane/echo", JSON, b'{"rank":"x"}'),
    ("POST", "/lane/echo", JSON, b"{nope"),
    ("POST", "/lane/echo", JSON, b""),
    ("POST", "/lane/echo", "content-type: text/plain\r\n", b"hello"),
    ("POST", "/lane/echo", "", b'{"title":"a"}'),
    ("PUT", "/lane/note/2", JSON, b'{"title":"b"}'),
    ("PUT", "/lane/note/404", JSON, b'{"title":"b"}'),
    ("PUT", "/lane/note/2", JSON, b"[]"),
    ("PATCH", "/lane/guarded/2", JSON + bearer("editor-token"), b'{"title":"c"}'),
    ("PATCH", "/lane/guarded/2", JSON + bearer("reader-token"), b'{"title":"c"}'),
    ("PATCH", "/lane/guarded/2", JSON, b'{"title":"c"}'),
    ("PATCH", "/lane/guarded/2", JSON + bearer("editor-token"), b"{nope"),
    ("QUERY", "/lane/find", JSON, b'{"title":"q"}'),
    ("POST", "/lane/limited-body", JSON, b'{"title":"a"}'),
    ("POST", "/lane/limited-body", JSON, b"{nope"),
]


async def test_body_routes_take_the_lane_like_the_asyncio_path(url: str) -> None:
    oracle, none = await serve_all(url, BODY_TARGETS, native=False)
    served, started = await serve_all(url, BODY_TARGETS, native=True)
    assert none == []
    assert [normalized(r) for r in served] == [normalized(r) for r in oracle]
    assert [r.status for r in served] == [
        201, 201, 422, 422, 400, 400, 415, 415, 200, 404, 422, 200, 403, 401, 400,
        200, 429, 429,
    ]  # fmt: skip
    assert served[1].json() == {"title": "a", "rank": 4, "total": 3}
    assert started.count(1) == len(BODY_TARGETS)


async def test_a_body_above_the_limit_is_refused_like_the_asyncio_path(
    url: str,
) -> None:
    big: Sent = ("POST", "/lane/echo", JSON, b'{"title":"' + b"x" * 600 + b'"}')
    results = []
    for native in (False, True):
        worker = KisoWorker(
            make_app(url, max_body_bytes=256), "127.0.0.1", 0, native_dispatch=native
        )
        port = await worker.start()
        try:
            method, target, extra, body = request_of(big)
            results.append(await fetch(port, method, target, extra, body=body))
        finally:
            await worker.stop()
    assert results[0].status == results[1].status == 413
    assert normalized(results[0]) == normalized(results[1])


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
