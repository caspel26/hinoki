"""Database tests against a disposable PostgreSQL.

They run only when HINOKI_TEST_DATABASE_URL is set (for example
``postgres://postgres:hinoki@127.0.0.1:54399/hinoki``) and are reported as
skipped otherwise, so a green run without a database proves nothing about the
database layer.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from hinoki.db import (
    Database,
    DatabaseError,
    ForeignKeyViolation,
    NotNullViolation,
    UniqueViolation,
)

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    URL is None, reason="HINOKI_TEST_DATABASE_URL is not set"
)

SCHEMA = """
CREATE TABLE {t} (
    id uuid PRIMARY KEY, slug text UNIQUE NOT NULL, n int,
    at timestamptz, data jsonb, flag bool, blob bytea, ratio float8
)
"""


@pytest.fixture
async def db(request: pytest.FixtureRequest) -> AsyncIterator[Database]:
    assert URL is not None
    database = await Database.connect(URL, max_size=3, wait=0.5)
    table = request.node.name.split("[")[0].replace("test_", "t_")[:40]
    await database.execute(f"DROP TABLE IF EXISTS {table}")
    await database.execute(SCHEMA.format(t=table))
    database.table = table  # type: ignore[attr-defined]
    yield database
    await database.execute(f"DROP TABLE IF EXISTS {table}")
    database.close()


def table(db: Database) -> str:
    return str(db.table)  # type: ignore[attr-defined]


async def add(db: Database, slug: str, n: int | None = 1) -> UUID:
    ident = uuid4()
    sql = f"INSERT INTO {table(db)} (id, slug, n) VALUES ($1, $2, $3)"
    await db.execute(sql, ident, slug, n)
    return ident


async def count(db: Database) -> int:
    row = await db.fetch_one(f"SELECT count(*)::int AS c FROM {table(db)}")
    assert row is not None
    return int(row["c"])


async def test_values_round_trip_with_python_types(db: Database) -> None:
    ident, now = uuid4(), datetime.now(UTC)
    sql = (
        f"INSERT INTO {table(db)} (id, slug, n, at, data, flag, blob, ratio) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)"
    )
    args = (ident, "a", 7, now, {"k": [1, 2]}, True, b"\x01\x02", 2.5)
    assert await db.execute(sql, *args) == 1
    row = await db.fetch_one(f"SELECT * FROM {table(db)} WHERE id = $1", ident)
    assert row is not None
    assert row["id"] == ident
    assert row["at"] == now
    assert row["data"] == {"k": [1, 2]}
    assert row["blob"] == b"\x01\x02"
    assert (row["n"], row["flag"], row["ratio"]) == (7, True, 2.5)


async def test_null_round_trips_and_missing_rows_are_none(db: Database) -> None:
    await add(db, "a", n=None)
    row = await db.fetch_one(f"SELECT n FROM {table(db)}")
    assert row == {"n": None}
    assert (
        await db.fetch_one(f"SELECT n FROM {table(db)} WHERE slug = $1", "zz") is None
    )


async def test_bad_parameters_fail_before_reaching_the_server(db: Database) -> None:
    sql = f"INSERT INTO {table(db)} (id, slug, n) VALUES ($1, $2, $3)"
    with pytest.raises(ValueError, match="timezone"):
        await db.execute("SELECT $1::timestamptz", datetime(2026, 1, 1))
    with pytest.raises(TypeError, match="unsupported parameter type"):
        await db.execute(sql, uuid4(), "a", object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="out of range"):
        await db.execute(sql, uuid4(), "a", 2**70)
    assert await count(db) == 0


async def test_no_implicit_casts_between_types(db: Database) -> None:
    sql = f"INSERT INTO {table(db)} (id, slug, n) VALUES ($1, $2, $3)"
    with pytest.raises(DatabaseError):
        await db.execute(sql, "not-a-uuid", "a", 1)
    with pytest.raises(DatabaseError):
        await db.execute(sql, uuid4(), "a", 2**40)  # exceeds int4, never truncated


async def test_constraint_errors_are_typed_and_carry_no_values(db: Database) -> None:
    await add(db, "very-secret-slug")
    with pytest.raises(UniqueViolation) as unique:
        await add(db, "very-secret-slug")
    assert "very-secret-slug" not in str(unique.value)
    assert unique.value.constraint == f"{table(db)}_slug_key"
    assert unique.value.table == table(db)
    with pytest.raises(NotNullViolation):
        await db.execute(
            f"INSERT INTO {table(db)} (id, slug) VALUES ($1, $2)", uuid4(), None
        )
    with pytest.raises(ForeignKeyViolation):
        await db.execute("CREATE TABLE IF NOT EXISTS fk_parent (id int PRIMARY KEY)")
        await db.execute(
            "CREATE TABLE IF NOT EXISTS fk_child (p int REFERENCES fk_parent(id))"
        )
        await db.execute("INSERT INTO fk_child (p) VALUES ($1)", 1)


async def test_transaction_commits_on_success_and_rolls_back_on_error(
    db: Database,
) -> None:
    async with db.transaction():
        await add(db, "kept")
    assert await count(db) == 1

    with pytest.raises(RuntimeError, match="boom"):
        async with db.transaction():
            await add(db, "gone")
            raise RuntimeError("boom")
    assert await count(db) == 1


async def test_a_failed_commit_or_statement_is_an_error_not_success(
    db: Database,
) -> None:
    await add(db, "dup")
    with pytest.raises(UniqueViolation):
        async with db.transaction():
            await add(db, "dup")
    assert await count(db) == 1


async def test_nested_scopes_use_savepoints(db: Database) -> None:
    async with db.transaction():
        await add(db, "outer")
        with pytest.raises(RuntimeError):
            async with db.transaction():
                await add(db, "inner-lost")
                raise RuntimeError
        async with db.transaction():
            await add(db, "inner-kept")
    rows = await db.fetch(f"SELECT slug FROM {table(db)} ORDER BY slug")
    assert [r["slug"] for r in rows] == ["inner-kept", "outer"]


async def test_uncommitted_rows_are_invisible_to_other_connections(
    db: Database,
) -> None:
    sql = f"SELECT count(*)::int AS c FROM {table(db)}"
    async with db.transaction():
        await add(db, "pending")
        assert (await db.fetch(sql))[0]["c"] == 1  # same connection
        assert (await db.engine.fetch(sql, []))[0]["c"] == 0  # another one
    assert (await db.engine.fetch(sql, []))[0]["c"] == 1


async def test_concurrent_use_of_one_transaction_is_refused(db: Database) -> None:
    async with db.transaction():
        results = await asyncio.gather(
            db.fetch("SELECT pg_sleep(0.3)"),
            db.fetch("SELECT 1 AS one"),
            return_exceptions=True,
        )
    assert any(isinstance(r, DatabaseError) for r in results)


async def test_timeout_during_a_query_rolls_back_and_keeps_the_pool_usable(
    db: Database,
) -> None:
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2), db.transaction():
            await add(db, "never-committed")
            await db.fetch("SELECT pg_sleep(5)")
    assert await count(db) == 0
    rows = await asyncio.wait_for(db.fetch("SELECT 1 AS one"), 5)
    assert rows == [{"one": 1}]


async def test_pool_exhaustion_times_out_with_a_database_error(db: Database) -> None:
    held = [await db.engine.begin() for _ in range(3)]
    with pytest.raises(DatabaseError, match="timed out"):
        await db.fetch("SELECT 1")
    for tx in held:
        await tx.rollback()
    assert await db.fetch("SELECT 1 AS one") == [{"one": 1}]


async def test_connecting_to_an_unreachable_server_fails_without_the_password() -> None:
    with pytest.raises(DatabaseError) as info:
        await Database.connect("postgres://user:topsecret@127.0.0.1:1/none", wait=0.3)
    assert "topsecret" not in str(info.value)


async def test_status_reports_pool_usage(db: Database) -> None:
    await db.fetch("SELECT 1")
    size, idle = db.status()
    assert size >= 1
    assert idle >= 1
