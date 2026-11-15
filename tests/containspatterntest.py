"""``contains`` filters bind a prebuilt LIKE pattern on PostgreSQL and SQLite."""

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from hinoki import core
from hinoki.db import Database

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
TITLES = ["Alpha", "50%_off", "back\\slash", "plain", "ÉCOLE", "x"]
# Needle -> titles it must match (case-insensitive, wildcards literal).
CASES = {
    "alp": ["Alpha"],
    "%": ["50%_off"],
    "_": ["50%_off"],
    "\\": ["back\\slash"],
    "0%_": ["50%_off"],
    "": TITLES,
    "ALPHA": ["Alpha"],
    "no-such": [],
}


def compile_contains(
    dialect: str, op: str, column: str, value: Any
) -> tuple[str, list[Any]]:
    return core.compile_query(
        "select",
        "t",
        columns=["id"],
        filters=[(column, op, [value])],
        dialect=dialect,
    )


def test_postgres_binds_the_escaped_pattern_and_no_sql_escaping() -> None:
    sql, params = compile_contains("postgres", "contains", "title", "50%_x\\")
    assert sql.endswith('WHERE "title"::text ILIKE $1')
    assert params == ["%50\\%\\_x\\\\%"]


def test_sqlite_binds_the_escaped_pattern_with_an_explicit_escape() -> None:
    sql, params = compile_contains("sqlite", "contains", "title", "a_b")
    assert sql.endswith("WHERE CAST(\"title\" AS TEXT) LIKE ?1 ESCAPE '\\'")
    assert params == ["%a\\_b%"]


def test_containsany_shares_one_pattern_parameter() -> None:
    sql, params = compile_contains("postgres", "containsany", "title,body", "a%")
    assert sql.endswith('("title"::text ILIKE $1 OR "body"::text ILIKE $1)')
    assert params == ["%a\\%%"]


def test_mysql_and_non_text_values_keep_the_escaping_sql() -> None:
    sql, params = compile_contains("mysql", "contains", "title", "a%")
    assert "REPLACE(" in sql
    assert params == ["a%"]
    sql, params = compile_contains("sqlite", "contains", "title", 5)
    assert "replace(replace(replace(?1" in sql
    assert params == [5]


@pytest.fixture(params=["sqlite", "postgres"])
async def database(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[Database]:
    if request.param == "sqlite":
        db = await Database.connect(f"sqlite://{tmp_path / 'c.sqlite3'}", max_size=2)
    elif URL is None:
        pytest.skip("HINOKI_TEST_DATABASE_URL is not set")
    else:
        db = await Database.connect(URL, max_size=2)
        await db.execute("DROP TABLE IF EXISTS contains_pattern")
    await db.execute(
        "CREATE TABLE contains_pattern (id INTEGER PRIMARY KEY, title TEXT)"
    )
    for number, title in enumerate(TITLES):
        sql = "INSERT INTO contains_pattern VALUES ($1, $2)"
        await db.execute(sql, number, title)
    yield db
    await db.execute("DROP TABLE contains_pattern")
    db.close()


async def titles(database: Database, op: str, column: str, needle: str) -> list[str]:
    sql, params = core.compile_query(
        "select",
        "contains_pattern",
        columns=["title"],
        filters=[(column, op, [needle])],
        order=[("id", False)],
        dialect=database.dialect,
    )
    return [row["title"] for row in await database.fetch(sql, *params)]


@pytest.mark.parametrize("needle", list(CASES))
async def test_contains_matches_literally_and_ignoring_case(
    database: Database, needle: str
) -> None:
    got = await titles(database, "contains", "title", needle)
    assert got == CASES[needle]


async def test_containsany_matches_any_listed_column(database: Database) -> None:
    got = await titles(database, "containsany", "title,title", "plai")
    assert got == ["plain"]
