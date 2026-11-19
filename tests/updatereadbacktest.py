"""``QuerySet.aupdate`` returns the rows it changed on every backend, including
rows that stop matching the filter because of the update."""

import os
from collections.abc import AsyncIterator
from pathlib import Path

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
