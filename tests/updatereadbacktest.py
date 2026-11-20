"""``QuerySet.aupdate`` returns the rows it changed on every backend, including
rows that stop matching the filter because of the update."""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from hinoki.db import Database
from hinoki.orm import Model, field, use_database
from hinoki.orm.migrations import create_table_sql, table_state

POSTGRES_URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
MYSQL_URL = os.environ.get("HINOKI_TEST_MYSQL_URL")
MARIADB_URL = os.environ.get("HINOKI_TEST_MARIADB_URL")


class Draft(Model, table="readback_drafts"):
    id: int = field(primary_key=True)
    status: str = field(max_length=20)


DATABASES = (
    pytest.param(("sqlite", None), id="sqlite"),
    pytest.param(
        ("postgres", POSTGRES_URL),
        marks=pytest.mark.skipif(POSTGRES_URL is None, reason="no PostgreSQL"),
        id="postgresql",
    ),
    pytest.param(
        ("mysql", MYSQL_URL),
        marks=pytest.mark.skipif(MYSQL_URL is None, reason="no MySQL"),
        id="mysql",
    ),
    pytest.param(
        ("mariadb", MARIADB_URL),
        marks=pytest.mark.skipif(MARIADB_URL is None, reason="no MariaDB"),
        id="mariadb",
    ),
)


@pytest.fixture(params=DATABASES)
async def database(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[Database]:
    dialect, url = request.param
    db = await Database.connect(url or f"sqlite:///{tmp_path / 'rb.db'}", max_size=2)
    await db.execute(f"DROP TABLE IF EXISTS {Draft.__table__}")
    await db.execute(create_table_sql(Draft.__table__, table_state(Draft), dialect))
    with use_database(db):
        yield db
    await db.execute(f"DROP TABLE IF EXISTS {Draft.__table__}")
    db.close()


async def test_update_returns_rows_that_leave_the_filter(database: Database) -> None:
    for number in (1, 2, 3):
        await Draft(id=number, status="draft" if number < 3 else "live").asave()

    rows = await Draft.filter(status="draft").aupdate({"status": "live"})

    assert sorted(row.id for row in rows) == [1, 2]
    assert {row.status for row in rows} == {"live"}


async def test_update_returns_nothing_when_no_row_matches(database: Database) -> None:
    await Draft(id=1, status="live").asave()

    assert await Draft.filter(status="draft").aupdate({"status": "live"}) == []


async def test_update_reads_many_rows_back_in_bounded_chunks(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    for number in range(1, 6):
        await Draft(id=number, status="draft").asave()
    monkeypatch.setattr("hinoki.orm.queryset.IN_CHUNK", 2)
    statements: list[str] = []
    original = database.fetch

    async def spy(sql: str, *params: Any) -> list[dict[str, object]]:
        statements.append(sql)
        return await original(sql, *params)

    monkeypatch.setattr(database, "fetch", spy)

    rows = await Draft.filter(status="draft").aupdate({"status": "live"})

    assert sorted(row.id for row in rows) == [1, 2, 3, 4, 5]
    if database.dialect in {"mysql", "mariadb"}:
        locks = [sql for sql in statements if sql.endswith("FOR UPDATE")]
        assert len(locks) == 1
        assert "ORDER BY" in locks[0]
        assert len([sql for sql in statements if "IN (" in sql]) == 3


async def test_a_write_from_another_task_joins_the_open_transaction(
    database: Database,
) -> None:
    await Draft(id=1, status="draft").asave()

    async with database.atomic():
        rows = await asyncio.create_task(
            Draft.filter(status="draft").aupdate({"status": "live"})
        )

    assert [row.id for row in rows] == [1]
    assert (await Draft.filter(id=1).aget()).status == "live"
