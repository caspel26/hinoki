"""Native awaitables resolve on the loop thread, whatever the loop does."""

import asyncio
import threading
from pathlib import Path

import pytest

from hinoki.db import Database, DatabaseError, UniqueViolation


async def open_db(tmp_path: Path, name: str = "native.sqlite3") -> Database:
    db = await Database.connect(f"sqlite://{tmp_path / name}", max_size=4)
    await db.execute("create table t (id integer primary key, v text unique)")
    return db


async def test_many_concurrent_calls_all_resolve(tmp_path: Path) -> None:
    db = await open_db(tmp_path)
    await asyncio.gather(
        *(db.execute("insert into t (v) values (?)", str(n)) for n in range(500))
    )
    rows = await asyncio.gather(
        *(db.fetch("select v from t where v = ?", str(n)) for n in range(500))
    )
    assert [r[0]["v"] for r in rows] == [str(n) for n in range(500)]
    db.close()


async def test_error_keeps_class_and_details(tmp_path: Path) -> None:
    db = await open_db(tmp_path)
    await db.execute("insert into t (v) values (?)", "a")
    with pytest.raises(UniqueViolation) as raised:
        await db.execute("insert into t (v) values (?)", "a")
    assert isinstance(raised.value, DatabaseError)
    db.close()


async def test_cancelled_call_leaves_the_pool_usable(tmp_path: Path) -> None:
    db = await open_db(tmp_path)
    for _ in range(50):
        task = asyncio.ensure_future(db.fetch("select 1 as x"))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert (await db.fetch("select 2 as x"))[0]["x"] == 2
    db.close()


async def fail_in_transaction(db: Database) -> None:
    async with db.transaction():
        await db.execute("insert into t (v) values (?)", "gone")
        raise RuntimeError


async def test_transaction_commit_and_rollback(tmp_path: Path) -> None:
    db = await open_db(tmp_path)
    async with db.transaction():
        await db.execute("insert into t (v) values (?)", "kept")
    with pytest.raises(RuntimeError):
        await fail_in_transaction(db)
    assert [r["v"] for r in await db.fetch("select v from t")] == ["kept"]
    db.close()


def test_many_sequential_loops(tmp_path: Path) -> None:
    async def one(n: int) -> int:
        db = await open_db(tmp_path, f"loop{n}.sqlite3")
        try:
            return int((await db.fetch("select 7 as x"))[0]["x"])
        finally:
            db.close()

    assert [asyncio.run(one(n)) for n in range(30)] == [7] * 30


def test_two_loops_in_two_threads(tmp_path: Path) -> None:
    results: dict[int, int] = {}

    async def one(n: int) -> None:
        db = await open_db(tmp_path, f"thread{n}.sqlite3")
        rows = await asyncio.gather(*(db.fetch("select ? as x", n) for _ in range(200)))
        results[n] = sum(r[0]["x"] for r in rows)
        db.close()

    threads = [threading.Thread(target=asyncio.run, args=(one(n),)) for n in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == {1: 200, 2: 400}


def test_loop_closed_with_calls_pending(tmp_path: Path) -> None:
    pending: list[asyncio.Future[object]] = []

    async def start() -> Database:
        db = await open_db(tmp_path, "pending.sqlite3")
        pending.extend(
            asyncio.ensure_future(db.fetch("select 1 as x")) for _ in range(20)
        )
        return db

    loop = asyncio.new_event_loop()
    db = loop.run_until_complete(start())
    loop.close()
    db.close()

    async def again() -> int:
        fresh = await open_db(tmp_path, "after.sqlite3")
        return int((await fresh.fetch("select 3 as x"))[0]["x"])

    assert asyncio.run(again()) == 3


def test_loop_without_add_reader_uses_the_fallback(tmp_path: Path) -> None:
    class NoReader(asyncio.SelectorEventLoop):
        def add_reader(self, *args: object) -> None:
            raise NotImplementedError

    async def run() -> int:
        db = await open_db(tmp_path, "fallback.sqlite3")
        rows = await asyncio.gather(*(db.fetch("select 5 as x") for _ in range(50)))
        db.close()
        return sum(r[0]["x"] for r in rows)

    loop = NoReader()
    try:
        assert loop.run_until_complete(run()) == 250
    finally:
        loop.close()
