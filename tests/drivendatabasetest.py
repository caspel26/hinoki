"""Database calls made while Rust drives a coroutine need no event loop."""

import asyncio
from pathlib import Path

import pytest

from hinoki import core
from hinoki.db import Database


async def test_database_calls_in_a_driven_coroutine_are_native_waits(
    tmp_path: Path,
) -> None:
    db = await Database.connect(f"sqlite://{tmp_path / 'driven.sqlite3'}", max_size=2)
    await db.execute("create table t (id integer primary key, v text)")
    await db.execute("insert into t (v) values (?)", "one")

    async def handler() -> object:
        async with db.atomic():
            await db.execute("insert into t (v) values (?)", "two")
            return await db.fetch("select v from t order by id")

    rows = await asyncio.to_thread(core._drive, handler())
    assert [row["v"] for row in rows] == ["one", "two"]
    assert not core.driving()
    assert len(await db.fetch("select v from t")) == 2


async def test_a_driven_rollback_discards_the_write(tmp_path: Path) -> None:
    db = await Database.connect(f"sqlite://{tmp_path / 'rollback.sqlite3'}")
    await db.execute("create table t (id integer primary key, v text)")

    async def handler() -> None:
        async with db.atomic():
            await db.execute("insert into t (v) values (?)", "x")
            raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await asyncio.to_thread(core._drive, handler())
    assert await db.fetch("select v from t") == []


async def test_a_driven_savepoint_nests_in_its_transaction(tmp_path: Path) -> None:
    db = await Database.connect(f"sqlite://{tmp_path / 'savepoint.sqlite3'}")
    await db.execute("create table t (id integer primary key, v text)")

    async def handler() -> None:
        async with db.atomic():
            await db.execute("insert into t (v) values (?)", "kept")
            try:
                async with db.atomic():
                    await db.execute("insert into t (v) values (?)", "dropped")
                    raise ValueError("inner")
            except ValueError:
                pass

    await asyncio.to_thread(core._drive, handler())
    assert [row["v"] for row in await db.fetch("select v from t")] == ["kept"]
