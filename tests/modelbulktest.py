"""``Model.bulk_create``/``bulk_update``/``bulk_delete`` and their ``a`` twins:
each runs in one transaction, so a failure leaves nothing behind."""

import itertools
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from hinoki import Schema
from hinoki.db import Database, UniqueViolation
from hinoki.errors import FieldError
from hinoki.orm import Model, field, use_database
from hinoki.orm.migrations import create_table_sql, table_state

POSTGRES_URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
MYSQL_URL = os.environ.get("HINOKI_TEST_MYSQL_URL")
MARIADB_URL = os.environ.get("HINOKI_TEST_MARIADB_URL")


IDS = itertools.count(1000)


class Tag(Model, table="bulk_tags"):
    id: int = field(primary_key=True, default_factory=lambda: next(IDS))
    name: str = field(max_length=20, unique=True)
    stars: int = 0


class TagIn(Schema):
    name: str
    stars: int = 0


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
    db = await Database.connect(url or f"sqlite:///{tmp_path / 'bulk.db'}", max_size=2)
    await db.execute(f"DROP TABLE IF EXISTS {Tag.__table__}")
    await db.execute(create_table_sql(Tag.__table__, table_state(Tag), dialect))
    with use_database(db):
        yield db
    await db.execute(f"DROP TABLE IF EXISTS {Tag.__table__}")
    db.close()


async def names() -> list[str]:
    return sorted(tag.name for tag in await Tag.all().order_by("name"))


async def test_bulk_create_inserts_schemas_and_dicts_and_returns_the_rows(
    database: Database,
) -> None:
    rows = await Tag.abulk_create([TagIn(name="a", stars=2), {"id": 2, "name": "b"}])

    assert [(row.name, row.stars) for row in rows] == [("a", 2), ("b", 0)]
    assert await names() == ["a", "b"]


async def test_bulk_create_with_nothing_does_nothing(database: Database) -> None:
    assert await Tag.abulk_create([]) == []


async def test_bulk_create_is_all_or_nothing(database: Database) -> None:
    with pytest.raises(UniqueViolation):
        await Tag.abulk_create(
            [{"id": 1, "name": "a"}, {"id": 2, "name": "a"}, {"id": 3, "name": "c"}]
        )

    assert await names() == []


async def test_bulk_create_checks_values_before_writing(database: Database) -> None:
    with pytest.raises(FieldError, match="nope"):
        await Tag.abulk_create([{"id": 1, "name": "a"}, {"id": 2, "nope": "x"}])

    assert await names() == []


async def test_bulk_update_saves_changed_rows_in_one_transaction(
    database: Database,
) -> None:
    await Tag.abulk_create([{"id": n, "name": f"t{n}"} for n in (1, 2, 3)])
    rows = await Tag.all().order_by("id")
    rows[0].stars = 5
    rows[1].stars = 7

    saved = await Tag.abulk_update(rows)

    assert [row.stars for row in saved] == [5, 7, 0]
    assert [tag.stars for tag in await Tag.all().order_by("id")] == [5, 7, 0]


async def test_bulk_update_rolls_back_when_one_row_fails(database: Database) -> None:
    await Tag.abulk_create([{"id": n, "name": f"t{n}"} for n in (1, 2)])
    rows = await Tag.all().order_by("id")
    rows[0].stars = 9
    rows[1].name = "t1"

    with pytest.raises(UniqueViolation):
        await Tag.abulk_update(rows)

    assert [tag.stars for tag in await Tag.all().order_by("id")] == [0, 0]


async def test_bulk_update_refuses_an_unsaved_instance(database: Database) -> None:
    with pytest.raises(FieldError, match="unsaved"):
        await Tag.abulk_update([Tag(id=1, name="a")])

    assert await names() == []


async def test_bulk_delete_takes_instances_or_primary_keys(database: Database) -> None:
    await Tag.abulk_create([{"id": n, "name": f"t{n}"} for n in (1, 2, 3, 4)])
    first = await Tag.aget(id=1)

    assert await Tag.abulk_delete([first, 2]) == 2
    assert await names() == ["t3", "t4"]
    assert await Tag.abulk_delete([]) == 0
    assert await Tag.abulk_delete([99]) == 0


async def test_bulk_delete_works_in_bounded_chunks(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    await Tag.abulk_create([{"id": n, "name": f"t{n}"} for n in range(1, 6)])
    monkeypatch.setattr("hinoki.orm.queryset.IN_CHUNK", 2)

    assert await Tag.abulk_delete([1, 2, 3, 4, 5]) == 5
    assert await names() == []


async def test_bulk_delete_refuses_an_unsaved_instance(database: Database) -> None:
    with pytest.raises(FieldError, match="unsaved"):
        await Tag.abulk_delete([Tag(id=1, name="a")])


def test_sync_twins_exist_for_sync_handlers() -> None:
    for name in ("bulk_create", "bulk_update", "bulk_delete"):
        assert callable(getattr(Tag, name))
