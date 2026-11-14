"""SQLite backend behavior against disposable, file-backed databases."""

import asyncio
import contextvars
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from hinoki.db import Database, DatabaseError, ForeignKeyViolation, UniqueViolation
from hinoki.errors import DestructiveMigration, MigrationError
from hinoki.orm import Model, field, foreign_key, use_database
from hinoki.orm.migrations import plan
from hinoki.orm.migrator import apply


class Note(Model, table="q045_notes"):
    id: UUID = field(primary_key=True)
    title: str = field(unique=True)
    active: bool
    created: datetime
    payload: dict[str, str]


class BeforeMigration(Model, table="q045_rebuild"):
    id: UUID = field(primary_key=True)
    name: str


class AfterMigration(Model, table="q045_rebuild"):
    id: UUID = field(primary_key=True)
    name: str
    note: str | None


class MigrationChild(Model, table="q045_child"):
    id: int = field(primary_key=True)
    parent_id: UUID = foreign_key(BeforeMigration, on_delete="restrict")


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = await Database.connect(f"sqlite://{tmp_path / 'test.sqlite3'}", max_size=2)
    await db.execute(
        "CREATE TABLE q045_notes ("
        '"id" UUID PRIMARY KEY, "title" TEXT UNIQUE NOT NULL, '
        '"active" BOOLEAN NOT NULL, "created" TIMESTAMPTZ NOT NULL, '
        '"payload" JSONB NOT NULL)'
    )
    yield db
    db.close()


async def test_sqlite_orm_roundtrip_and_query_operations(database: Database) -> None:
    identifier = UUID("56a92e87-34ac-4c1a-93bb-ef319318a503")
    created = datetime(2026, 11, 14, 18, 30, tzinfo=UTC)
    with use_database(database):
        note = await Note(
            id=identifier,
            title="first",
            active=True,
            created=created,
            payload={"kind": "example"},
        ).insert()
        loaded = await Note.filter(active=True, title__contains="fir").aget()
        assert loaded.id == identifier
        assert loaded.created == created
        assert loaded.payload == {"kind": "example"}
        assert await Note.filter(id=identifier).acount() == 1
        changed = await Note.filter(id=identifier).aupdate({"active": False})
        assert changed[0].active is False
        assert await Note.filter(id=identifier).adelete() == 1
    assert note.title == "first"
    assert database.dialect == "sqlite"


async def test_sqlite_maps_constraint_errors_and_rolls_back(
    database: Database,
) -> None:
    with use_database(database):
        first = Note(
            id=UUID(int=1),
            title="duplicate",
            active=True,
            created=datetime(2026, 11, 14, tzinfo=UTC),
            payload={},
        )
        await first.insert()
        second = Note(
            id=UUID(int=2),
            title="duplicate",
            active=True,
            created=datetime(2026, 11, 14, tzinfo=UTC),
            payload={},
        )
        with pytest.raises(UniqueViolation):
            await second.insert()
        with pytest.raises(RuntimeError):
            await _raise_after_update(database)
        row = await database.fetch_one(
            'SELECT "title" FROM "q045_notes" WHERE "id" = ?1', UUID(int=1)
        )
        assert row == {"title": "duplicate"}


async def _raise_after_update(database: Database) -> None:
    async with database.transaction():
        await database.execute(
            'UPDATE "q045_notes" SET "title" = ?1 WHERE "id" = ?2',
            "rolled back",
            UUID(int=1),
        )
        raise RuntimeError("rollback")


async def test_sqlite_migration_rebuild_preserves_existing_rows(
    tmp_path: Path,
) -> None:
    database = await Database.connect(f"sqlite://{tmp_path / 'migration.sqlite3'}")
    migrations = tmp_path / "migrations"
    plan([BeforeMigration, MigrationChild], migrations, "initial", dialect="sqlite")
    await apply(database, migrations)
    identifier = UUID("56a92e87-34ac-4c1a-93bb-ef319318a503")
    await database.execute(
        'INSERT INTO "q045_rebuild" ("id", "name") VALUES (?1, ?2)',
        identifier,
        "preserved",
    )
    await database.execute(
        'INSERT INTO "q045_child" ("id", "parent_id") VALUES (?1, ?2)',
        1,
        identifier,
    )

    next_plan = plan(
        [AfterMigration, MigrationChild], migrations, "add-note", dialect="sqlite"
    )
    assert next_plan.operations[0].description == "rebuild table q045_rebuild"
    assert "INSERT INTO" in next_plan.operations[0].sql
    await apply(database, migrations)
    rows = await database.fetch(
        'SELECT "id", "name", "note" FROM "q045_rebuild" WHERE "id" = ?1',
        identifier,
    )
    assert rows == [{"id": identifier, "name": "preserved", "note": None}]
    child = await database.fetch(
        'SELECT "parent_id" FROM "q045_child" WHERE "id" = ?1', 1
    )
    assert child == [{"parent_id": identifier}]
    assert await database.fetch("PRAGMA foreign_key_check") == []
    database.close()


async def test_sqlite_migration_failure_rolls_back_and_restores_foreign_keys(
    tmp_path: Path,
) -> None:
    url = f"sqlite://{tmp_path / 'failed-migration.sqlite3'}"
    database = await Database.connect(url)
    assert (await database.fetch("PRAGMA foreign_keys"))[0]["foreign_keys"] == 1
    async with database.migration():
        assert (await database.fetch("PRAGMA foreign_keys"))[0]["foreign_keys"] == 0
    migrations = tmp_path / "failed-migrations"
    migrations.mkdir()
    (migrations / "0001-invalid.sql").write_text(
        "CREATE TABLE q045_parent (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE q045_orphan (parent_id INTEGER REFERENCES q045_parent(id));\n"
        "INSERT INTO q045_orphan (parent_id) VALUES (99);\n",
        encoding="utf-8",
    )

    with pytest.raises(MigrationError, match="foreign key constraints"):
        await apply(database, migrations)

    tables = await database.fetch(
        "SELECT name FROM sqlite_master WHERE name = ?1", "q045_parent"
    )
    assert tables == []
    assert (await database.fetch("PRAGMA foreign_keys"))[0]["foreign_keys"] == 1
    await database.execute("CREATE TABLE q045_parent (id INTEGER PRIMARY KEY)")
    await database.execute(
        "CREATE TABLE q045_orphan (parent_id INTEGER REFERENCES q045_parent(id))"
    )
    with pytest.raises(ForeignKeyViolation):
        await database.execute("INSERT INTO q045_orphan (parent_id) VALUES (99)")
    database.close()


async def test_sqlite_pool_wait_is_bounded_and_recovers(tmp_path: Path) -> None:
    database = await Database.connect(
        f"sqlite://{tmp_path / 'bounded-pool.sqlite3'}", max_size=1, wait=0.05
    )
    async with database.transaction():
        child = asyncio.create_task(
            database.fetch_one("SELECT 1"), context=contextvars.Context()
        )
        with pytest.raises(DatabaseError, match="timed out waiting"):
            await child
    assert await database.fetch_one("SELECT 1") == {"1": 1}
    database.close()


def test_sqlite_destructive_rebuild_requires_explicit_approval(tmp_path: Path) -> None:
    plan([AfterMigration], tmp_path, "initial", dialect="sqlite")
    with pytest.raises(DestructiveMigration, match="dropping note loses its data"):
        plan([BeforeMigration], tmp_path, "drop-note", dialect="sqlite")
