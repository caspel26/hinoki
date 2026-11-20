"""The native lane serves provable ``async def`` handlers without an asyncio
task. The asyncio path (``native_dispatch=False``) is the oracle: both must
answer every request alike."""

import asyncio
import os
import re
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, ClassVar

import pytest

from hinoki import (
    API,
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
from hinoki.runtime.servercore import stock_owner
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


@http.get("/lane/atomic/{id}", atomic=True)
async def atomic_read(id: int) -> dict[str, Any]:
    found = await LaneNote.aget(id=id)
    return {"id": found.id, "open": Holder.current[-1].active() is not None}


@http.post("/lane/atomic", status=201, atomic=True)
async def atomic_write(data: NewNote) -> dict[str, Any]:
    return {"title": data.title, "open": Holder.current[-1].active() is not None}


@http.post("/lane/limited-body", status=201, rate_limit=RefuseAll())
async def limited_body(data: NewNote) -> dict[str, str]:
    return {"title": data.title}


class Rename(Schema):
    title: str


@http.post("/lane/write/{id}", status=201)
async def write_create(id: int, data: Rename) -> dict[str, Any]:
    created = await LaneNote.acreate(id=id, title=data.title)
    return {"id": created.id, "title": created.title}


@http.put("/lane/write/{id}")
async def write_rename(id: int, data: Rename) -> dict[str, Any]:
    updated = await LaneNote.aupdate(id=id, title=data.title)
    return {"id": updated.id, "title": updated.title}


@http.delete("/lane/write/{id}")
async def write_remove(id: int) -> dict[str, int]:
    return {"deleted": await LaneNote.adelete(id=id)}


@http.patch("/lane/write")
async def write_many(data: Rename) -> dict[str, Any]:
    changed = await LaneNote.filter(id__in=[1, 2]).aupdate({"title": data.title})
    return {"changed": sorted(row.id for row in changed)}


@http.post("/lane/write-bulk", status=201)
async def write_bulk(data: Rename) -> dict[str, int]:
    rows = await LaneNote.abulk_create(
        [{"id": 20, "title": data.title}, {"id": 21, "title": data.title}]
    )
    return {"created": len(rows)}


@http.post("/lane/write-bulk-clash", status=201)
async def write_bulk_clash(data: Rename) -> dict[str, int]:
    rows = await LaneNote.abulk_create(
        [{"id": 30, "title": data.title}, {"id": 1, "title": data.title}]
    )
    return {"created": len(rows)}


@http.post("/lane/write-atomic/{id}", status=201, atomic=True)
async def write_atomic(id: int, data: Rename) -> dict[str, int]:
    await LaneNote.acreate(id=id, title=data.title)
    await LaneNote.aget(id=999)
    return {"id": id}


@http.post("/lane/write-atomic-bulk/{id}", status=201, atomic=True)
async def write_atomic_bulk(id: int, data: Rename) -> dict[str, int]:
    rows = await LaneNote.abulk_create(
        [{"id": id, "title": data.title}, {"id": id + 1, "title": data.title}]
    )
    await LaneNote.abulk_create([{"id": id + 2, "title": data.title}])
    await LaneNote.aget(id=999 if id == 50 else 1)
    return {"created": len(rows)}


@http.post("/lane/write-atomic-ok/{id}", status=201, atomic=True)
async def write_atomic_ok(id: int, data: Rename) -> dict[str, int]:
    await LaneNote.acreate(id=id, title=data.title)
    await LaneNote.adelete(id=1)
    return {"id": id}


class LaneResource(API, path="/lane/res"):
    @http.get("/{id}")
    async def one(self, id: int) -> dict[str, Any]:
        found = await LaneNote.aget(id=id)
        return {"id": found.id, "title": found.title}

    @http.get("/boom")
    async def boom(self) -> dict[str, str]:
        raise ValueError("boom")

    @http.post("/make", status=201)
    async def make(self, data: Rename) -> dict[str, int]:
        made = await LaneNote.acreate(id=40, title=data.title)
        return {"id": made.id}


class BuiltResource(API, path="/lane/built"):
    def __init__(self) -> None:
        self.label = "built"

    @http.get("/{id}")
    async def one(self, id: int) -> dict[str, Any]:
        found = await LaneNote.aget(id=id)
        return {"id": found.id, "title": found.title, "label": self.label}


class HeaderedResource(API, path="/lane/headered"):
    response_headers = (("x-lane-owner", "yes"),)

    @http.get("/{id}")
    async def one(self, id: int) -> dict[str, Any]:
        found = await LaneNote.aget(id=id)
        return {"id": found.id, "title": found.title}


HANDLERS = (note, notes, count, pure, seen_context, boom, missing, sleeping, plain)
CLASSES = (LaneResource, BuiltResource, HeaderedResource)
GUARDED = (guarded, slow_auth, limited, limited_sync, search, negotiated)
BODIES = (echo, replace, amend, find, atomic_read, atomic_write, limited_body)
WRITES = (
    write_create,
    write_rename,
    write_remove,
    write_many,
    write_bulk,
    write_bulk_clash,
    write_atomic,
    write_atomic_ok,
    write_atomic_bulk,
)
SERVED = {"/lane/note/{id}", "/lane/notes", "/lane/count", "/lane/pure"}
SERVED |= {"/lane/context", "/lane/boom", "/lane/not-found"}
SERVED |= {"/lane/guarded/{id}", "/lane/slow-auth", "/lane/limited"}
SERVED |= {"/lane/limited-sync", "/lane/search", "/lane/negotiated"}
SERVED |= {"/lane/echo", "/lane/limited-body", "/lane/find"}
SERVED |= {"/lane/atomic/{id}", "/lane/atomic"}
SERVED |= {"/lane/write/{id}", "/lane/write", "/lane/write-bulk"}
SERVED |= {"/lane/write-bulk-clash", "/lane/write-atomic/{id}"}
SERVED |= {"/lane/write-atomic-ok/{id}", "/lane/write-atomic-bulk/{id}"}
SERVED |= {"/lane/res/{id}", "/lane/res/boom", "/lane/res/make"}


def make_app(url: str, **runtime: float) -> Hinoki:
    app = Hinoki(runtime=RuntimeConfig(**runtime))  # type: ignore[arg-type]
    app.include(*HANDLERS, *GUARDED, *BODIES, *WRITES, *CLASSES)
    app.install(DatabaseModule(url), Holder())
    return app


def normalized(received: Received) -> tuple[int, list[tuple[str, str]], bytes]:
    """Without the request id, the one value that differs per request."""
    pattern = re.compile(rb"[0-9a-f]{32}")
    headers = [(k, v) for k, v in received.headers if k not in {"x-request-id", "date"}]
    return received.status, headers, pattern.sub(b"<id>", received.body)


Sent = str | tuple[str, str] | tuple[str, str, str] | tuple[str, str, str, bytes]


def request_of(sent: Sent) -> tuple[str, str, str, bytes]:
    """The method, target, extra headers and body of a request to send."""
    if isinstance(sent, str):
        return "GET", sent, "", b""
    if len(sent) == 2:
        return "GET", sent[0], sent[1], b""
    if len(sent) == 3:
        return sent[0], sent[1], sent[2], b""
    method, target, extra, body = sent
    return method, target, f"{extra}content-length: {len(body)}\r\n", body


async def serve_all(
    url: str,
    targets: Sequence[Sent],
    *,
    native: bool,
    after_start: Callable[[], None] | None = None,
) -> tuple[list[Received], list[int]]:
    worker = KisoWorker(make_app(url), "127.0.0.1", 0, native_dispatch=native)
    port = await worker.start()
    if after_start is not None:
        after_start()
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


JSON = "content-type: application/json\r\n"
ATOMIC_TARGETS: list[Sent] = [
    "/lane/atomic/1",
    "/lane/atomic/404",
    ("POST", "/lane/atomic", JSON, b'{"title":"t"}'),
    ("POST", "/lane/atomic", JSON, b'{"title":3}'),
]


async def test_atomic_routes_run_in_a_transaction_in_the_lane(url: str) -> None:
    oracle, none = await serve_all(url, ATOMIC_TARGETS, native=False)
    served, started = await serve_all(url, ATOMIC_TARGETS, native=True)
    assert none == []
    assert [normalized(r) for r in served] == [normalized(r) for r in oracle]
    assert [r.status for r in served] == [200, 404, 201, 422]
    assert served[0].json() == {"id": 1, "open": True}
    assert served[2].json() == {"title": "t", "open": True}
    assert started == [1, 1, 1, 1]


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


WRITE_TARGETS: list[Sent] = [
    ("POST", "/lane/write/10", JSON, b'{"title":"ten"}'),
    ("POST", "/lane/write/10", JSON, b'{"title":"again"}'),
    ("GET", "/lane/note/10", ""),
    ("PUT", "/lane/write/10", JSON, b'{"title":"renamed"}'),
    ("PUT", "/lane/write/99", JSON, b'{"title":"nobody"}'),
    ("GET", "/lane/note/10", ""),
    ("PATCH", "/lane/write", JSON, b'{"title":"both"}'),
    ("DELETE", "/lane/write/10", ""),
    ("DELETE", "/lane/write/10", ""),
    ("GET", "/lane/note/10", ""),
    ("POST", "/lane/write-bulk", JSON, b'{"title":"bulk"}'),
    ("POST", "/lane/write-bulk", JSON, b'{"title":"bulk"}'),
    ("POST", "/lane/write-bulk-clash", JSON, b'{"title":"clash"}'),
    ("GET", "/lane/note/30", ""),
    ("POST", "/lane/write-atomic/40", JSON, b'{"title":"gone"}'),
    ("GET", "/lane/note/40", ""),
    ("POST", "/lane/write-atomic-ok/41", JSON, b'{"title":"kept"}'),
    ("GET", "/lane/note/41", ""),
    ("GET", "/lane/note/1", ""),
    ("GET", "/lane/count", ""),
    ("POST", "/lane/write/11", JSON, b"{nope"),
]


async def test_writes_take_the_lane_like_the_asyncio_path(url: str) -> None:
    oracle, none = await serve_all(url, WRITE_TARGETS, native=False)
    served, started = await serve_all(url, WRITE_TARGETS, native=True)
    assert none == []
    assert [normalized(r) for r in served] == [normalized(r) for r in oracle]
    statuses = [r.status for r in served]
    assert statuses[:4] == [201, 409, 200, 200]
    assert served[3].json() == {"id": 10, "title": "renamed"}
    assert statuses[4] == 404
    assert statuses[12] == 409
    assert served[13].status == 404
    assert served[15].status == 404
    assert served[17].json() == {"id": 41, "title": "kept"}
    assert served[18].status == 404
    assert started == [1] * len(WRITE_TARGETS)


HOOK_TARGETS: list[Sent] = [
    ("POST", "/lane/write-atomic-bulk/60", JSON, b'{"title":"kept"}'),
    ("GET", "/lane/note/61", ""),
    ("POST", "/lane/write-atomic-bulk/50", JSON, b'{"title":"gone"}'),
    ("GET", "/lane/note/50", ""),
    ("GET", "/lane/note/51", ""),
    ("POST", "/lane/write/70", JSON, b'{"title":"hooked"}'),
    ("PUT", "/lane/write/70", JSON, b'{"title":"hooked again"}'),
    ("GET", "/lane/note/70", ""),
]


async def test_a_hook_added_after_startup_hands_over_without_losing_the_transaction(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proof saw no hook, so the request starts in the lane; the hook awaits
    asyncio, the request moves to a task in the middle of the transaction, and
    savepoints opened after that must still nest and roll back."""
    calls: list[int] = []

    async def after_save(self: LaneNote) -> None:
        calls.append(self.id)
        await asyncio.sleep(0)

    def hook() -> None:
        monkeypatch.setattr(LaneNote, "after_save", after_save)

    oracle, none = await serve_all(url, HOOK_TARGETS, native=False, after_start=hook)
    monkeypatch.undo()
    oracle_calls, calls[:] = list(calls), []
    served, started = await serve_all(url, HOOK_TARGETS, native=True, after_start=hook)
    assert none == []
    assert [normalized(r) for r in served] == [normalized(r) for r in oracle]
    assert calls == oracle_calls
    assert [r.status for r in served] == [201, 200, 404, 404, 404, 201, 200, 200]
    assert started == [1] * len(HOOK_TARGETS)


async def test_class_api_handlers_take_the_lane_like_the_asyncio_path(
    url: str,
) -> None:
    targets: list[Sent] = [
        "/lane/res/1",
        "/lane/res/9",
        "/lane/res/x",
        "/lane/res/boom",
        ("POST", "/lane/res/make", JSON, b'{"title":"t"}'),
        ("POST", "/lane/res/make", JSON, b'{"title":3}'),
        "/lane/built/1",
        "/lane/headered/1",
        "/lane/headered/9",
    ]
    oracle, _ = await serve_all(url, targets, native=False)
    served, started = await serve_all(url, targets, native=True)
    assert [normalized(r) for r in served] == [normalized(r) for r in oracle]
    assert served[0].json() == {"id": 1, "title": "one"}
    assert served[6].json()["label"] == "built"
    assert served[7].header("x-lane-owner") == "yes"
    assert served[8].status == 404 and served[8].header("x-lane-owner") == "yes"
    # one, one (not found), boom and both makes take the lane (the invalid body
    # is refused inside it); the invalid path does not, nor does a class that
    # builds itself with application code or one that adds response headers.
    assert started == [1, 1, 0, 1, 1, 1, 0, 0, 0]


def test_only_a_class_with_the_stock_constructor_is_a_native_owner() -> None:
    assert stock_owner(LaneResource)
    assert not stock_owner(BuiltResource)

    class Slotted(API):
        def __new__(cls) -> "Slotted":
            return super().__new__(cls)

    assert not stock_owner(Slotted)


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
