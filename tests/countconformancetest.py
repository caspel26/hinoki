"""Row counts and pagination totals on every supported database server."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hinoki.db import Database
from hinoki.orm import Model, field, use_database
from hinoki.pagination import PageParams

POSTGRES_URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
MYSQL_URL = os.environ.get("HINOKI_TEST_MYSQL_URL")
MARIADB_URL = os.environ.get("HINOKI_TEST_MARIADB_URL")


class CountedItem(Model, table="count_conformance_items"):
    id: int = field(primary_key=True)
    name: str


DATABASES = (
    pytest.param(None, "sqlite", id="sqlite"),
    pytest.param(
        POSTGRES_URL,
        "postgres",
        marks=pytest.mark.skipif(
            POSTGRES_URL is None, reason="HINOKI_TEST_DATABASE_URL is not set"
        ),
        id="postgresql",
    ),
    pytest.param(
        MYSQL_URL,
        "mysql",
        marks=pytest.mark.skipif(
            MYSQL_URL is None, reason="HINOKI_TEST_MYSQL_URL is not set"
        ),
        id="mysql",
    ),
    pytest.param(
        MARIADB_URL,
        "mariadb",
        marks=pytest.mark.skipif(
            MARIADB_URL is None, reason="HINOKI_TEST_MARIADB_URL is not set"
        ),
        id="mariadb",
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("url", "dialect"), DATABASES)
async def test_count_and_page_total_use_the_same_column_name(
    url: str | None, dialect: str, tmp_path: Path
) -> None:
    if dialect == "sqlite":
        url = f"sqlite://{tmp_path / 'count.sqlite3'}"
    assert url is not None
    database = await Database.connect(url, max_size=2)
    try:
        await database.execute("DROP TABLE IF EXISTS count_conformance_items")
        await database.execute(
            "CREATE TABLE count_conformance_items "
            "(id integer PRIMARY KEY, name varchar(20) NOT NULL)"
        )
        await database.execute(
            "INSERT INTO count_conformance_items (id, name) VALUES "
            "(1, 'item-1'), (2, 'item-2'), (3, 'item-3'), (4, 'item-4'), (5, 'item-5')"
        )
        with use_database(database):
            assert await CountedItem.all().acount() == 5
            assert await CountedItem.all().filter(id__gt=3).acount() == 2
            page = (
                await CountedItem.all()
                .order_by("id")
                .apaginate(PageParams(page=2, page_size=2))
            )
            assert page.total == 5
            assert [item.id for item in page.items] == [3, 4]
        await database.execute("DROP TABLE count_conformance_items")
    finally:
        database.close()
