"""``Model.bulk_create``/``bulk_update``/``bulk_delete`` and their ``a`` twins:
each runs in one transaction, so a failure leaves nothing behind."""

import asyncio
import itertools
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from hinoki import Schema, context, db
from hinoki.db import Database, UniqueViolation
from hinoki.errors import FieldError
from hinoki.orm import Model, current_database, field, use_database
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


async def in_thread[T](call: Callable[[], T]) -> T:
    """Run ``call`` as a sync handler would: on a worker thread whose context
    points at this loop, so the sync model calls reach the database."""
    token = context.event_loop.set(asyncio.get_running_loop())
    try:
        return await asyncio.to_thread(call)
    finally:
        context.event_loop.reset(token)


def failing_sync_bulk() -> None:
    with db.atomic():
        Tag.bulk_create([{"id": 9, "name": "undone"}])
        raise ZeroDivisionError


async def test_sync_twins_run_inside_an_open_sync_transaction(
    database: Database,
) -> None:
    def work() -> None:
        with db.atomic():
            rows = Tag.bulk_create([{"id": n, "name": f"t{n}"} for n in (1, 2, 3)])
            rows[0].stars = 4
            Tag.bulk_update(rows)
            Tag.bulk_delete([rows[2]])
        with pytest.raises(ZeroDivisionError):
            failing_sync_bulk()

    await in_thread(work)

    assert [(tag.name, tag.stars) for tag in await Tag.all().order_by("id")] == [
        ("t1", 4),
        ("t2", 0),
    ]


async def test_a_failed_sync_bulk_in_an_open_transaction_is_a_savepoint(
    database: Database,
) -> None:
    def work() -> None:
        with db.atomic():
            Tag.bulk_create([{"id": 1, "name": "kept"}])
            with pytest.raises(UniqueViolation):
                Tag.bulk_create([{"id": 2, "name": "new"}, {"id": 3, "name": "kept"}])
            Tag.bulk_create([{"id": 4, "name": "after"}])

    await in_thread(work)

    assert await names() == ["after", "kept"]


async def test_a_sync_bulk_without_a_transaction_is_all_or_nothing(
    database: Database,
) -> None:
    def work() -> None:
        with pytest.raises(UniqueViolation):
            Tag.bulk_create([{"id": 1, "name": "a"}, {"id": 2, "name": "a"}])

    await in_thread(work)

    assert await names() == []


class Audited(Model, table="bulk_audited"):
    id: int = field(primary_key=True)

    async def after_create(self) -> None:
        async with db.atomic():
            await current_database().execute(
                "UPDATE bulk_audited SET id = id WHERE id = ?", self.id
            )


async def test_a_sync_bulk_lets_a_hook_open_its_own_scope(tmp_path: Path) -> None:
    opened = await Database.connect(f"sqlite:///{tmp_path / 'hooks.db'}", max_size=2)
    await opened.execute(
        create_table_sql(Audited.__table__, table_state(Audited), "sqlite")
    )
    try:
        with use_database(opened):
            await in_thread(lambda: Audited.bulk_create([{"id": 1}, {"id": 2}]))
            assert [row.id for row in await Audited.all().order_by("id")] == [1, 2]
    finally:
        opened.close()


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda: Tag.bulk_create([]), id="create-empty"),
        pytest.param(lambda: Tag.bulk_update([]), id="update-empty"),
        pytest.param(lambda: Tag.bulk_delete([]), id="delete-empty"),
        pytest.param(lambda: Tag.bulk_create([{"bogus": 1}]), id="create-invalid"),
        pytest.param(lambda: Tag.bulk_update([Tag(name="a")]), id="update-unsaved"),
    ],
)
async def test_a_sync_bulk_on_the_event_loop_is_refused_before_anything_else(
    call: Callable[[], object],
) -> None:
    with pytest.raises(RuntimeError, match=r"await the a\.\.\. variant"):
        call()


async def test_bulk_update_with_nothing_does_nothing(database: Database) -> None:
    assert await Tag.abulk_update([]) == []


async def test_a_sync_bulk_with_nothing_writes_nothing(database: Database) -> None:
    await Tag.abulk_create([{"id": 1, "name": "keep"}])

    assert await in_thread(lambda: Tag.bulk_create([])) == []
    assert await in_thread(lambda: Tag.bulk_update([])) == []
    assert await in_thread(lambda: Tag.bulk_delete([])) == 0
    assert await names() == ["keep"]
