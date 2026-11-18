"""``db.atomic``: an atomic block or coroutine function on the current database."""

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest

from hinoki import Hinoki, RouteGroup, Schema, Transactions, db, http
from hinoki.db import Database, DatabaseModule
from hinoki.orm import current_database, use_database
from hinoki.rules import Project, RuleSet
from hinoki.runtime.rustserver import RustWorker
from hinoki.testing import ASGIClient
from tests.rustservertest import fetch, record_lanes

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


def test_a_sync_function_is_refused_when_decorated() -> None:
    def sync(name: str) -> str:
        return name

    with pytest.raises(TypeError, match="coroutine functions only"):
        db.atomic(sync)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="coroutine functions only"):
        db.atomic()(sync)  # type: ignore[arg-type]


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
        RouteGroup("/g").post("/", **{"transaction": True})
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
    worker = RustWorker(app_for(url), "127.0.0.1", 0)
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
