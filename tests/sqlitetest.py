"""SQLite backend behavior against disposable, file-backed databases."""

import asyncio
import contextvars
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from hinoki import context
from hinoki.db import Database, DatabaseError, ForeignKeyViolation, UniqueViolation
from hinoki.errors import (
    DestructiveMigration,
    FieldError,
    MigrationError,
    MultipleObjectsReturned,
)
from hinoki.orm import Model, OnDelete, field, foreign_key, use_database
from hinoki.orm.migrations import create_table_sql, plan, table_state
from hinoki.orm.migrator import apply, apply_one, status
from hinoki.pagination import PageParams


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
    parent_id: UUID = foreign_key(BeforeMigration, on_delete=OnDelete.RESTRICT)


class BeforeTypeMigration(Model, table="q045_type_rebuild"):
    id: UUID = field(primary_key=True)
    payload: bytes
    count: int | None


class AfterTypeMigration(Model, table="q045_type_rebuild"):
    id: UUID = field(primary_key=True)
    payload: bytes
    count: float | None


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
    async with database.atomic():
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


async def test_sqlite_type_rebuild_uses_sqlite_types_and_preserves_blob_data(
    tmp_path: Path,
) -> None:
    database = await Database.connect(f"sqlite://{tmp_path / 'type-rebuild.sqlite3'}")
    migrations = tmp_path / "type-migrations"
    plan([BeforeTypeMigration], migrations, "initial", dialect="sqlite")
    await apply(database, migrations)
    identifier = UUID("56a92e87-34ac-4c1a-93bb-ef319318a503")
    await database.execute(
        'INSERT INTO "q045_type_rebuild" ("id", "payload", "count") '
        "VALUES (?1, ?2, ?3)",
        identifier,
        b"\x00\x01payload",
        7,
    )

    rebuilt = plan(
        [AfterTypeMigration],
        migrations,
        "change-count-type",
        dialect="sqlite",
        allow_destructive=True,
    )
    sql = "\n".join(operation.sql for operation in rebuilt.operations)
    assert "ALTER COLUMN" not in sql and "bytea" not in sql
    assert '"payload" blob' in sql and '"count" real' in sql
    await apply(database, migrations)

    columns = await database.fetch("PRAGMA table_info(q045_type_rebuild)")
    types = {column["name"]: column["type"] for column in columns}
    assert types["payload"] == "BLOB" and types["count"] == "REAL"
    rows = await database.fetch(
        'SELECT "payload", "count" FROM "q045_type_rebuild" WHERE "id" = ?1',
        identifier,
    )
    assert rows == [{"payload": b"\x00\x01payload", "count": 7.0}]
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
    async with database.atomic():
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


async def test_sqlite_status_tells_applied_pending_changed_and_missing(
    database: Database, tmp_path: Path
) -> None:
    first = tmp_path / "0001-first.sql"
    first.write_text("CREATE TABLE q045_m1 (id INTEGER);\n", encoding="utf-8")
    assert await status(database, tmp_path) == [("0001-first", "pending")]

    assert await apply(database, tmp_path) == ["0001-first"]
    assert await status(database, tmp_path) == [("0001-first", "applied")]

    first.write_text("CREATE TABLE q045_m1 (id INTEGER, x TEXT);\n", encoding="utf-8")
    assert await status(database, tmp_path) == [("0001-first", "changed")]
    with pytest.raises(MigrationError, match="0001-first is changed"):
        await apply(database, tmp_path)

    first.unlink()
    assert await status(database, tmp_path) == [("0001-first", "missing")]
    with pytest.raises(MigrationError, match="0001-first is missing"):
        await apply(database, tmp_path)


async def test_sqlite_applying_a_recorded_file_again_does_nothing(
    database: Database, tmp_path: Path
) -> None:
    path = tmp_path / "0001-once.sql"
    path.write_text("CREATE TABLE q045_once (id INTEGER);\n", encoding="utf-8")

    await status(database, tmp_path)  # creates the ledger
    assert await apply_one(database, path) is True
    assert await apply_one(database, path) is False


async def test_sqlite_a_failing_statement_rolls_the_file_back_unrecorded(
    database: Database, tmp_path: Path
) -> None:
    (tmp_path / "0001-broken.sql").write_text(
        "CREATE TABLE q045_broken (id INTEGER);\nSELECT * FROM q045_nope;\n",
        encoding="utf-8",
    )

    with pytest.raises(MigrationError, match="0001-broken failed and was rolled back"):
        await apply(database, tmp_path)

    assert (
        await database.fetch(
            "SELECT name FROM sqlite_master WHERE name = 'q045_broken'"
        )
        == []
    )
    assert await status(database, tmp_path) == [("0001-broken", "pending")]


async def test_sqlite_status_reports_pool_usage(database: Database) -> None:
    await database.fetch("SELECT 1")

    size, idle = database.status()

    assert size >= 1 and idle >= 1


async def test_sqlite_a_task_that_outlives_its_transaction_cannot_use_it(
    database: Database,
) -> None:
    async def late() -> object:
        await asyncio.sleep(0.05)
        return await database.fetch("SELECT 1")

    async with database.atomic():
        straggler = asyncio.create_task(late())

    with pytest.raises(DatabaseError, match="has ended"):
        await straggler


async def test_sqlite_savepoints_must_be_opened_by_the_owning_task(
    database: Database,
) -> None:
    async def sibling() -> None:
        async with database.atomic():
            pass

    async with database.atomic():
        with pytest.raises(DatabaseError, match="owns the transaction"):
            await asyncio.create_task(sibling())


async def test_sqlite_a_failed_commit_is_an_error_and_leaves_nothing_behind(
    database: Database,
) -> None:
    await database.execute("CREATE TABLE q045_dp (id INTEGER PRIMARY KEY)")
    await database.execute(
        "CREATE TABLE q045_dc (parent_id INTEGER REFERENCES q045_dp(id) "
        "DEFERRABLE INITIALLY DEFERRED)"
    )

    with pytest.raises(ForeignKeyViolation):
        async with database.atomic():
            await database.execute("INSERT INTO q045_dc (parent_id) VALUES (99)")

    assert await database.fetch("SELECT * FROM q045_dc") == []
    assert await database.fetch_one("SELECT 1") == {"1": 1}


class Author(Model, table="q045_authors"):
    id: int = field(primary_key=True)
    name: str = field(unique=True)


class Book(Model, table="q045_books"):
    id: int = field(primary_key=True)
    author_id: int | None = foreign_key(
        Author, on_delete=OnDelete.SET_NULL, default=None, related_name="books"
    )
    title: str = ""


class Passport(Model, table="q045_passports"):
    id: int = field(primary_key=True)
    author_id: int = foreign_key(
        Author, on_delete=OnDelete.CASCADE, unique=True, related_name="passport"
    )


@pytest.fixture
async def library(database: Database) -> AsyncIterator[Database]:
    for model in (Author, Book, Passport):
        await database.execute(
            create_table_sql(model.__table__, table_state(model), "sqlite")
        )
    with use_database(database):
        yield database


async def in_thread[T](call: Callable[[], T]) -> T:
    """Run ``call`` as a sync handler would: on a worker thread whose context
    points at this loop."""
    token = context.event_loop.set(asyncio.get_running_loop())
    try:
        return await asyncio.to_thread(call)
    finally:
        context.event_loop.reset(token)


def sync_model_calls() -> dict[str, object]:
    seen: dict[str, object] = {}
    seen["created"] = Author.create(id=1, name="a", returning=False).name
    seen["read_back"] = Author.create(id=2, name="b").name
    seen["get"] = Author.get(id=1).name
    seen["missing"] = Author.get_or_none(id=99)
    seen["where_count"] = Author.update_where({"id": 1}, {"name": "A"}, returning=False)
    seen["where_rows"] = [r.name for r in Author.update_where({"id": 2}, {"name": "B"})]
    seen["update_count"] = Author.update(id=1, name="x", returning=False)
    seen["update_row"] = Author.update(id=2, name="y").name
    seen["first"] = Author.all().order_by("id").first()
    seen["queryset_get"] = Author.filter(id=2).get().name
    seen["queryset_count"] = Author.filter(id=2).update({"name": "z"}, returning=False)
    seen["queryset_rows"] = [r.name for r in Author.filter(id=2).update({"name": "w"})]
    seen["count"] = Author.all().count()
    seen["exists"] = Author.filter(name="w").exists()
    page = Author.all().order_by("id").paginate(PageParams(1, 1))
    seen["page"] = [r.id for r in page.items]
    saved = Author.get(id=1)
    saved.name = "saved"
    seen["saved"] = saved.save().name
    seen["queryset_deleted"] = Author.filter(id=2).delete()
    seen["deleted"] = Author.delete(id=1)
    return seen


async def test_sqlite_sync_twins_reach_the_database_from_a_worker_thread(
    library: Database,
) -> None:
    seen = await in_thread(sync_model_calls)

    first = seen.pop("first")
    assert isinstance(first, Author) and first.name == "x"
    assert seen == {
        "created": "a",
        "read_back": "b",
        "get": "a",
        "missing": None,
        "where_count": 1,
        "where_rows": ["B"],
        "update_count": 1,
        "update_row": "y",
        "queryset_get": "y",
        "queryset_count": 1,
        "queryset_rows": ["w"],
        "count": 2,
        "exists": True,
        "page": [1],
        "saved": "saved",
        "queryset_deleted": 1,
        "deleted": 1,
    }
    assert await Author.all().acount() == 0


async def test_sqlite_get_refuses_several_matches(library: Database) -> None:
    await Author.acreate(id=1, name="a")
    await Author.acreate(id=2, name="b")

    with pytest.raises(MultipleObjectsReturned, match="several rows match"):
        await Author.all().aget()


@pytest.mark.parametrize("window", ["limit", "offset"])
async def test_sqlite_bulk_writes_refuse_a_window(
    library: Database, window: str
) -> None:
    await Author.acreate(id=1, name="a")
    windowed = (
        Author.filter(id=1).limit(1)
        if window == "limit"
        else Author.filter(id=1).offset(1)
    )

    with pytest.raises(FieldError, match="does not apply limit/offset"):
        await windowed.aupdate({"name": "b"})
    with pytest.raises(FieldError, match="does not apply limit/offset"):
        await windowed.adelete()


async def test_sqlite_adelete_without_conditions_is_refused(library: Database) -> None:
    await Author.acreate(id=1, name="a")

    with pytest.raises(FieldError, match="adelete needs at least one condition"):
        await Author.adelete()

    assert await Author.all().acount() == 1


async def test_sqlite_the_primary_key_of_a_saved_row_cannot_be_edited(
    library: Database,
) -> None:
    author = await Author.acreate(id=1, name="a")
    author.id = 5

    with pytest.raises(FieldError, match="primary key cannot be changed"):
        await author.asave()


async def test_sqlite_a_failed_update_from_leaves_the_instance_as_it_was(
    library: Database,
) -> None:
    await Author.acreate(id=1, name="taken")
    other = await Author.acreate(id=2, name="other")

    with pytest.raises(UniqueViolation):
        await other.aupdate_from(name="taken")

    assert other.name == "other"


async def test_sqlite_prefetch_loads_forward_and_one_to_one_relations(
    library: Database,
) -> None:
    ada = await Author.acreate(id=1, name="ada")
    await Book.acreate(id=1, author_id=ada.id, title="owned")
    await Book.acreate(id=2, title="orphan")
    await Passport.acreate(id=1, author_id=ada.id)

    books = await Book.all().order_by("id").prefetch_related("author")
    authors = await Author.all().prefetch_related("passport", "books")

    assert [b.related("author") and b.related("author").name for b in books] == [
        "ada",
        None,
    ]
    assert authors[0].related("passport").author_id == ada.id
    assert [b.title for b in authors[0].related("books")] == ["owned"]


async def test_sqlite_prefetch_refuses_a_one_to_one_with_two_rows(
    library: Database,
) -> None:
    ada = await Author.acreate(id=1, name="ada")
    await library.execute("DROP TABLE q045_passports")
    await library.execute(
        "CREATE TABLE q045_passports (id INTEGER PRIMARY KEY, author_id INTEGER)"
    )
    await Passport.acreate(id=1, author_id=ada.id)
    await Passport.acreate(id=2, author_id=ada.id)

    with pytest.raises(MultipleObjectsReturned, match="at most one Passport"):
        await Author.all().prefetch_related("passport")
