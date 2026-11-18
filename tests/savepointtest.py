"""Nested transactions (savepoints) on every supported server."""

import os
from collections.abc import AsyncIterator

import pytest

from hinoki.db import Database, UniqueViolation

URLS = (
    pytest.param(
        os.environ.get("HINOKI_TEST_DATABASE_URL"),
        marks=pytest.mark.skipif(
            os.environ.get("HINOKI_TEST_DATABASE_URL") is None,
            reason="HINOKI_TEST_DATABASE_URL is not set",
        ),
        id="postgresql",
    ),
    pytest.param(
        os.environ.get("HINOKI_TEST_MYSQL_URL"),
        marks=pytest.mark.skipif(
            os.environ.get("HINOKI_TEST_MYSQL_URL") is None,
            reason="HINOKI_TEST_MYSQL_URL is not set",
        ),
        id="mysql",
    ),
    pytest.param(
        os.environ.get("HINOKI_TEST_MARIADB_URL"),
        marks=pytest.mark.skipif(
            os.environ.get("HINOKI_TEST_MARIADB_URL") is None,
            reason="HINOKI_TEST_MARIADB_URL is not set",
        ),
        id="mariadb",
    ),
)
TABLE = "savepoint_items"


@pytest.fixture(params=URLS)
async def database(request: pytest.FixtureRequest) -> AsyncIterator[Database]:
    db = await Database.connect(request.param, max_size=2, wait=2)
    await db.execute(f"DROP TABLE IF EXISTS {TABLE}")
    await db.execute(f"CREATE TABLE {TABLE} (id bigint PRIMARY KEY, name varchar(20))")
    try:
        yield db
    finally:
        await db.execute(f"DROP TABLE IF EXISTS {TABLE}")
        db.close()


async def nested(db: Database, sql: str, fail: bool = False) -> None:
    """Run ``sql`` in a nested transaction, which fails afterwards when asked."""
    async with db.transaction():
        await db.execute(sql)
        if fail:
            raise ZeroDivisionError


async def names(db: Database) -> list[str]:
    rows = await db.fetch(f"SELECT name FROM {TABLE} ORDER BY id")  # noqa: S608
    return [row["name"] for row in rows]


async def test_a_nested_transaction_that_succeeds_commits_with_the_outer_one(
    database: Database,
) -> None:
    async with database.transaction():
        await database.execute(f"INSERT INTO {TABLE} VALUES (1, 'outer')")  # noqa: S608
        async with database.transaction():
            await database.execute(f"INSERT INTO {TABLE} VALUES (2, 'inner')")  # noqa: S608
    assert await names(database) == ["outer", "inner"]


async def test_a_failed_nested_transaction_rolls_back_only_itself(
    database: Database,
) -> None:
    async with database.transaction():
        await database.execute(f"INSERT INTO {TABLE} VALUES (1, 'outer')")  # noqa: S608
        with pytest.raises(ZeroDivisionError):
            await nested(database, f"INSERT INTO {TABLE} VALUES (2, 'inner')", True)  # noqa: S608
        await database.execute(f"INSERT INTO {TABLE} VALUES (3, 'after')")  # noqa: S608
    assert await names(database) == ["outer", "after"]


async def test_a_constraint_error_leaves_the_transaction_usable(
    database: Database,
) -> None:
    async with database.transaction():
        await database.execute(f"INSERT INTO {TABLE} VALUES (1, 'first')")  # noqa: S608
        with pytest.raises(UniqueViolation):
            await nested(database, f"INSERT INTO {TABLE} VALUES (1, 'again')")  # noqa: S608
        await database.execute(f"INSERT INTO {TABLE} VALUES (2, 'second')")  # noqa: S608
    assert await names(database) == ["first", "second"]


async def test_a_statement_without_values_may_be_a_savepoint(
    database: Database,
) -> None:
    async with database.transaction():
        await database.execute(f"INSERT INTO {TABLE} VALUES (1, 'kept')")  # noqa: S608
        await database.execute("SAVEPOINT manual_sp")
        await database.execute(f"INSERT INTO {TABLE} VALUES (2, 'undone')")  # noqa: S608
        await database.execute("ROLLBACK TO SAVEPOINT manual_sp")
        await database.execute("RELEASE SAVEPOINT manual_sp")
    assert await names(database) == ["kept"]
