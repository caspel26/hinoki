"""``db.atomic``: an atomic block or coroutine function on the current database."""

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest

from hinoki import Hinoki, RouteGroup, Schema, context, db, errors, http
from hinoki.blocking import run_sync
from hinoki.db import Database, DatabaseModule, current
from hinoki.orm import current_database, use_database
from hinoki.rules import Project, RuleSet, Transactions
from hinoki.runtime.kiso import KisoWorker
from hinoki.sessions import (
    MemorySessionStore,
    Session,
    SessionModule,
    SessionRecord,
    SessionSettings,
)
from hinoki.testing import ASGIClient
from tests.kisoservertest import fetch, record_lanes

TABLE = "atomic_items"
INSERT = f"INSERT INTO {TABLE} (name) VALUES (?)"  # noqa: S608


class Item(Schema):
    name: str


class Saved(Schema):
    name: str


@pytest.fixture
def url(tmp_path: Path) -> str:
    return f"sqlite://{tmp_path / 'atomic.sqlite3'}"


@pytest.fixture
async def database(url: str) -> AsyncIterator[Database]:
    opened = await Database.connect(url, max_size=2)
    await opened.execute(f"CREATE TABLE {TABLE} (name text PRIMARY KEY)")
    try:
        with use_database(opened):
            yield opened
    finally:
        opened.close()


async def names(database: Database) -> list[str]:
    rows = await database.fetch(f"SELECT name FROM {TABLE} ORDER BY name")  # noqa: S608
    return [row["name"] for row in rows]


async def insert(name: str) -> None:
    await current_database().execute(INSERT, name)


async def failing_block(
    name: str, through: Callable[[str], Awaitable[object]] = insert
) -> None:
    """Write ``name`` in an atomic block that then raises."""
    async with db.atomic():
        await through(name)
        raise ZeroDivisionError


@db.atomic
async def add(name: str, *, fail: bool = False) -> str:
    """Insert ``name``; raise afterwards when asked."""
    await insert(name)
    if fail:
        raise ZeroDivisionError
    return name


@db.atomic()
async def add_with_parentheses(name: str, *, fail: bool = False) -> str:
    await insert(name)
    if fail:
        raise ZeroDivisionError
    return name


async def test_a_block_commits_on_success_and_rolls_back_on_an_exception(
    database: Database,
) -> None:
    async with db.atomic() as scope:
        assert database.active() is scope.state
        await insert("kept")
    with pytest.raises(ZeroDivisionError):
        await failing_block("undone")
    assert await names(database) == ["kept"]
    assert database.active() is None


@pytest.mark.parametrize("decorated", [add, add_with_parentheses])
async def test_a_decorated_function_commits_on_return_and_rolls_back_on_raise(
    database: Database, decorated: object
) -> None:
    assert callable(decorated)
    assert await decorated("a") == "a"
    with pytest.raises(ZeroDivisionError):
        await decorated("b", fail=True)
    assert await names(database) == ["a"]


async def test_each_call_of_a_decorated_function_opens_its_own_scope(
    database: Database,
) -> None:
    await add("first")
    await add("second")
    assert await names(database) == ["first", "second"]
    assert database.active() is None


async def test_a_nested_call_is_a_savepoint_of_the_open_scope(
    database: Database,
) -> None:
    async with db.atomic():
        await insert("outer")
        with pytest.raises(ZeroDivisionError):
            await add("inner", fail=True)  # rolls back only its savepoint
        await add("sibling")
        async with db.atomic():  # a nested block is a savepoint too
            await insert("nested")
    assert await names(database) == ["nested", "outer", "sibling"]


class Recording:
    """A native transaction that records statements and can fail a release."""

    def __init__(self, native: object, fail_release: bool) -> None:
        self.native = native
        self.fail_release = fail_release
        self.statements: list[str] = []

    async def execute(self, sql: str, params: list[object]) -> object:
        self.statements.append(sql)
        if self.fail_release and sql.startswith("RELEASE"):
            self.fail_release = False
            raise ConnectionError("release lost")
        return await self.native.execute(sql, params)  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        return getattr(self.native, name)


async def test_a_savepoint_is_released_after_its_rollback(database: Database) -> None:
    async with db.atomic():
        state = database.active()
        assert state is not None
        recording = Recording(state.native, fail_release=False)
        state.native = recording  # type: ignore[assignment]
        with pytest.raises(ZeroDivisionError):
            await add("inner", fail=True)
    assert recording.statements[-2:] == [
        "ROLLBACK TO SAVEPOINT hinoki_sp_1",
        "RELEASE SAVEPOINT hinoki_sp_1",
    ]


async def remembered(database: Database, ran: list[str]) -> None:
    async with db.atomic():
        await insert("inner")
        database.after_commit(lambda: ran.append("inner"))


async def test_a_failed_release_drops_the_blocks_after_commit_callbacks(
    database: Database,
) -> None:
    ran: list[str] = []
    async with db.atomic():
        database.after_commit(lambda: ran.append("outer"))
        state = database.active()
        assert state is not None
        recording = Recording(state.native, fail_release=True)
        state.native = recording  # type: ignore[assignment]
        with pytest.raises(ConnectionError):
            await remembered(database, ran)
        await insert("outer")
    assert ran == ["outer"]
    assert await names(database) == ["outer"]
    assert recording.statements[-3:-1] == [
        "ROLLBACK TO SAVEPOINT hinoki_sp_1",
        "RELEASE SAVEPOINT hinoki_sp_1",
    ]


async def test_an_outer_rollback_undoes_the_committed_savepoints(
    database: Database,
) -> None:
    with pytest.raises(ZeroDivisionError):
        await failing_block("inner", through=add)
    assert await names(database) == []


async def test_one_atomic_object_can_be_entered_again_while_open(
    database: Database,
) -> None:
    scope = db.atomic()
    async with scope, scope:
        await insert("twice")
    assert await names(database) == ["twice"]
    with pytest.raises(RuntimeError, match="not entered"):
        await scope.__aexit__(None, None, None)


def test_the_decorator_keeps_the_function_metadata_and_signature() -> None:
    async def original(name: str, *, fail: bool = False) -> str:
        return name

    assert add.__name__ == "add"
    assert add.__doc__ == "Insert ``name``; raise afterwards when asked."
    assert inspect.iscoroutinefunction(add)
    assert str(inspect.signature(add)) == str(inspect.signature(original))
    assert inspect.unwrap(add) is not add


async def in_thread[T](call: Callable[[], T]) -> T:
    """Run ``call`` as a sync handler would: on a worker thread whose context
    points at this loop, so the sync model calls reach the database."""
    token = context.event_loop.set(asyncio.get_running_loop())
    try:
        return await asyncio.to_thread(call)
    finally:
        context.event_loop.reset(token)


def sync_insert(name: str) -> None:
    run_sync(insert(name))


@db.atomic
def sync_add(name: str, *, fail: bool = False) -> str:
    sync_insert(name)
    if fail:
        raise ZeroDivisionError
    return name


def test_sync_decorated_function_keeps_metadata_and_stays_plain() -> None:
    assert sync_add.__name__ == "sync_add"
    assert not inspect.iscoroutinefunction(sync_add)
    assert inspect.unwrap(sync_add) is not sync_add


def failing_sync_block(name: str) -> None:
    with db.atomic():
        sync_insert(name)
        raise ZeroDivisionError


async def test_a_sync_block_commits_and_rolls_back_on_a_worker_thread(
    database: Database,
) -> None:
    def work() -> None:
        with db.atomic():
            sync_insert("kept")
        assert current.get() is None
        with pytest.raises(ZeroDivisionError):
            failing_sync_block("undone")

    await in_thread(work)
    assert await names(database) == ["kept"]


async def test_a_sync_decorated_function_commits_and_rolls_back(
    database: Database,
) -> None:
    def work() -> str:
        with pytest.raises(ZeroDivisionError):
            sync_add("b", fail=True)
        return sync_add("a")

    assert await in_thread(work) == "a"
    assert await names(database) == ["a"]


async def test_a_nested_sync_block_is_a_savepoint(database: Database) -> None:
    def work() -> None:
        with db.atomic():
            sync_insert("outer")
            with pytest.raises(ZeroDivisionError):
                failing_sync_block("inner")
            sync_insert("after")

    await in_thread(work)
    assert await names(database) == ["after", "outer"]


async def test_a_sync_block_refuses_to_run_on_the_event_loop(
    database: Database,
) -> None:
    with pytest.raises(RuntimeError, match="event loop"), db.atomic():
        pass
    assert database.active() is None


async def test_without_a_current_database_entering_fails_clearly() -> None:
    with pytest.raises(RuntimeError, match="no database is active"):
        async with db.atomic():
            pass
    with pytest.raises(RuntimeError, match="no database is active"):
        await add("x")


def test_the_old_route_option_name_is_rejected() -> None:
    with pytest.raises(TypeError):
        http.post("/x", **{"transaction": True})  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="unknown action options"):
        RouteGroup("/g").post("/", **{"transaction": True})  # type: ignore[arg-type]
    assert not hasattr(Database, "transaction")


@http.post("/items", status=201)
@db.atomic
async def create_item(data: Item) -> Saved:
    await insert(data.name)
    if data.name == "boom":
        raise RuntimeError("after the write")
    return Saved(name=data.name)


def app_for(url: str) -> Hinoki:
    return Hinoki(include=[create_item], modules=[DatabaseModule(url)])


async def test_a_decorated_function_serves_as_a_route_handler(
    database: Database, url: str
) -> None:
    async with ASGIClient(app_for(url)) as client:
        created = await client.post("/items", json={"name": "a"})
        failed = await client.post("/items", json={"name": "boom"})
    assert (created.status, created.body) == (201, {"name": "a"})
    assert failed.status == 500
    assert await names(database) == ["a"]


def test_the_route_keeps_the_handler_signature_for_openapi(url: str) -> None:
    document = app_for(url).openapi()
    operation = document["paths"]["/items"]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Item"
    }
    assert "201" in operation["responses"]


def test_the_atomic_rule_reads_the_route_option_not_the_decorator(url: str) -> None:
    app = app_for(url)
    assert app.routes[0].atomic is False
    found = RuleSet(Transactions.require_atomic()).run(Project(app))
    assert [d.rule_id for d in found] == ["HNK-TXN-001"]
    assert "does not declare atomic=True" in found[0].message


async def test_a_decorated_function_rolls_back_on_the_native_lane(
    database: Database, url: str
) -> None:
    worker = KisoWorker(app_for(url), "127.0.0.1", 0)
    port = await worker.start()
    try:
        lanes = record_lanes(worker)
        for name in ("native", "boom"):
            body = f'{{"name": "{name}"}}'.encode()
            extra = f"content-type: application/json\r\ncontent-length: {len(body)}\r\n"
            received = await fetch(port, "POST", "/items", extra, body=body)
            assert received.status == (201 if name == "native" else 500)
        assert lanes == [True, True]
    finally:
        await worker.stop()
    assert await names(database) == ["native"]


class FakeScope:
    def __init__(self, name: str, log: list[tuple[str, bool]]) -> None:
        self.name = name
        self.log = log

    async def __aenter__(self) -> "FakeScope":
        return self

    async def __aexit__(self, exc_type: object, *rest: object) -> None:
        self.log.append((self.name, exc_type is not None))


async def test_one_atomic_object_keeps_a_scope_stack_per_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[tuple[str, bool]] = []
    names_iter = iter(["a", "b"])
    monkeypatch.setattr(
        "hinoki.db.current_scope", lambda: FakeScope(next(names_iter), log)
    )
    shared = db.atomic()
    first_in = asyncio.Event()
    second_in = asyncio.Event()

    async def first() -> None:
        async with shared:
            first_in.set()
            await second_in.wait()
            raise ValueError

    async def second() -> None:
        await first_in.wait()
        async with shared:
            second_in.set()
            await asyncio.sleep(0.05)

    await asyncio.gather(first(), second(), return_exceptions=True)
    assert sorted(log) == [("a", True), ("b", False)]


@http.post("/sync-items", status=201)
def create_sync_item(data: Item) -> Saved:
    with db.atomic():
        sync_insert(data.name)
        if data.name == "boom":
            raise RuntimeError("after the write")
    return Saved(name=data.name)


async def test_a_sync_handler_can_use_a_block_in_its_own_code(
    database: Database, url: str
) -> None:
    app = Hinoki(include=[create_sync_item], modules=[DatabaseModule(url)])
    async with ASGIClient(app) as client:
        created = await client.post("/sync-items", json={"name": "a"})
        failed = await client.post("/sync-items", json={"name": "boom"})
    assert (created.status, created.body) == (201, {"name": "a"})
    assert failed.status == 500
    assert await names(database) == ["a"]


async def test_atomic_stream_response_rolls_back_before_error_translation(
    database: Database, url: str, tmp_path: Path
) -> None:
    payload = tmp_path / "payload.txt"
    payload.write_text("streamed")

    @http.post("/atomic-file", atomic=True)
    async def atomic_file() -> http.Response:
        await insert("leaked")
        return http.File(payload)

    app = Hinoki(include=[atomic_file], modules=[DatabaseModule(url)])
    async with ASGIClient(app) as client:
        reply = await client.post("/atomic-file")
    assert reply.status == 500
    assert await names(database) == []


async def test_atomic_explicit_4xx_response_commits_as_normal_return(
    database: Database, url: str
) -> None:
    @http.post("/atomic-409", atomic=True)
    async def atomic_conflict() -> http.Response:
        await insert("kept-409")
        return http.Response(409, b"conflict")

    app = Hinoki(include=[atomic_conflict], modules=[DatabaseModule(url)])
    async with ASGIClient(app) as client:
        reply = await client.post("/atomic-409")
    assert reply.status == 409
    assert await names(database) == ["kept-409"]


async def test_atomic_declared_error_rolls_back(database: Database, url: str) -> None:
    @http.post("/atomic-conflict", atomic=True, errors=(errors.Conflict,))
    async def atomic_error() -> Saved:
        await insert("undone-conflict")
        raise errors.Conflict("already exists")

    app = Hinoki(include=[atomic_error], modules=[DatabaseModule(url)])
    async with ASGIClient(app) as client:
        reply = await client.post("/atomic-conflict")
    assert reply.status == 409
    assert await names(database) == []


class FailingSaveStore(MemorySessionStore):
    async def save(self, token_hash: str, record: SessionRecord) -> None:
        raise RuntimeError("session store unavailable")


async def test_atomic_sql_commits_before_session_store_save(
    database: Database, url: str
) -> None:
    @http.post("/atomic-session", atomic=True)
    async def atomic_session(session: Session) -> Saved:
        await insert("sql-committed")
        session["step"] = "done"
        return Saved(name="sql-committed")

    settings = SessionSettings(csrf_secret=b"t" * 32)
    app = Hinoki(
        include=[atomic_session],
        modules=[DatabaseModule(url), SessionModule(FailingSaveStore(), settings)],
    )
    async with ASGIClient(app) as client:
        reply = await client.post("/atomic-session")
    assert reply.status == 500
    assert await names(database) == ["sql-committed"]


async def test_atomic_stream_source_closes_after_rollback(
    database: Database, url: str
) -> None:
    events: list[tuple[str, bool]] = []

    class RecordingStream:
        def __aiter__(self) -> "RecordingStream":
            return self

        async def __anext__(self) -> bytes:
            raise StopAsyncIteration

        async def aclose(self) -> None:
            events.append(("aclose", current_database().active() is None))

    @http.post("/atomic-stream-close", atomic=True)
    async def atomic_stream() -> http.Response:
        await insert("leaked-stream")
        return http.Stream(RecordingStream())

    app = Hinoki(include=[atomic_stream], modules=[DatabaseModule(url)])
    async with ASGIClient(app) as client:
        reply = await client.post("/atomic-stream-close")
    assert reply.status == 500
    assert await names(database) == []
    assert events == [("aclose", True)]
