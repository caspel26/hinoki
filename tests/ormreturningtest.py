"""``returning=False`` on the ORM writes: nothing is read back, the default
still returns the stored rows, and sync twins and dialects behave alike."""

import os
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from hinoki import Hinoki, http
from hinoki.db import Database, DatabaseModule
from hinoki.errors import ModelValidationError, RegistrationError
from hinoki.http.request import Request
from hinoki.orm import Model, field, timestamps, use_database
from hinoki.utils import utc_now
from tests.harness import Client, Pool

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
needs_database = pytest.mark.skipif(
    URL is None, reason="HINOKI_TEST_DATABASE_URL is not set"
)
TABLE = "ret_notes"
COLUMNS = (
    "id uuid PRIMARY KEY, title text NOT NULL, stars int NOT NULL, "
    "data jsonb NOT NULL, updated_at timestamptz NOT NULL"
)
# Freezing the clock makes the client-side update stamp predictable.
STAMP = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)


@timestamps(clock=lambda: STAMP)
class Note(Model, table=TABLE):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str = field(max_length=40)
    stars: int = 0
    data: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=utc_now)


class Recorder:
    """Every statement a database runs, with the method that ran it."""

    def __init__(self, database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, str]] = []
        for method in ("fetch", "execute"):
            monkeypatch.setattr(database, method, self.wrap(method, database))

    def wrap(self, method: str, database: Database) -> Callable[..., Any]:
        original = getattr(database, method)

        async def recorded(sql: str, *params: Any) -> Any:
            self.calls.append((method, sql))
            return await original(sql, *params)

        return recorded

    def take(self) -> list[tuple[str, str]]:
        calls, self.calls = self.calls, []
        return calls


@pytest.fixture
async def postgres() -> AsyncIterator[Database]:
    assert URL is not None
    database = await Database.connect(URL, max_size=2)
    await database.execute(f"DROP TABLE IF EXISTS {TABLE}")
    await database.execute(f"CREATE TABLE {TABLE} ({COLUMNS})")
    with use_database(database):
        yield database
    await database.execute(f"DROP TABLE IF EXISTS {TABLE}")
    await database.execute("DROP FUNCTION IF EXISTS ret_notes_upper()")
    database.close()


@pytest.fixture
async def sqlite(tmp_path: Path) -> AsyncIterator[Database]:
    database = await Database.connect(f"sqlite://{tmp_path / 'returning.sqlite3'}")
    await database.execute(
        f'CREATE TABLE {TABLE} ("id" UUID PRIMARY KEY, "title" TEXT NOT NULL, '
        '"stars" INTEGER NOT NULL, "data" JSONB NOT NULL, '
        '"updated_at" TIMESTAMPTZ NOT NULL)'
    )
    with use_database(database):
        yield database
    database.close()


def only_execute_without_returning(calls: list[tuple[str, str]], kind: str) -> None:
    assert len(calls) == 1
    method, sql = calls[0]
    assert method == "execute"
    assert sql.upper().startswith(kind)
    assert "RETURNING" not in sql.upper()


def only_fetch_with_returning(calls: list[tuple[str, str]], kind: str) -> None:
    assert len(calls) == 1
    method, sql = calls[0]
    assert method == "fetch"
    assert sql.upper().startswith(kind)
    assert "RETURNING" in sql.upper()


async def check_default_reads_the_stored_rows(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder(database, monkeypatch)
    note = await Note.acreate(title="a", data={"k": 1})
    only_fetch_with_returning(recorder.take(), "INSERT")
    updated = await Note.aupdate(id=note.id, stars=2)
    only_fetch_with_returning(recorder.take(), "UPDATE")
    assert isinstance(updated, Note)
    assert (updated.title, updated.stars, updated.updated_at) == ("a", 2, STAMP)
    rows = await Note.aupdate_where({"id": note.id}, {"stars": 3})
    assert [row.stars for row in rows] == [3]
    rows = await Note.filter(title="a").aupdate({"stars": 4})
    assert [row.stars for row in rows] == [4]
    only_fetch_with_returning(recorder.calls[:1], "UPDATE")


async def check_opt_out_reads_nothing_back(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder(database, monkeypatch)
    note = await Note.acreate(title="b", data={"k": [1, 2]}, returning=False)
    only_execute_without_returning(recorder.take(), "INSERT")
    assert isinstance(note, Note)
    assert (note.title, note.stars, note.data) == ("b", 0, {"k": [1, 2]})
    assert await Note.aupdate(id=note.id, stars=5, returning=False) == 1
    only_execute_without_returning(recorder.take(), "UPDATE")
    assert await Note.aupdate(id=uuid4(), stars=5, returning=False) == 0
    recorder.take()
    count = await Note.aupdate_where({"id": note.id}, {"stars": 6}, returning=False)
    assert count == 1
    only_execute_without_returning(recorder.take(), "UPDATE")
    await Note.acreate(title="b", returning=False)
    recorder.take()
    assert await Note.filter(title="b").aupdate({"stars": 7}, returning=False) == 2
    only_execute_without_returning(recorder.take(), "UPDATE")
    stored = await Note.aget(id=note.id)
    assert (stored.stars, stored.data, stored.updated_at) == (7, {"k": [1, 2]}, STAMP)


async def check_opt_out_instance_saves_as_an_update() -> None:
    note = Note(title="c")
    assert await note.insert(returning=False) is note
    note.stars = 9
    await note.asave()  # the snapshot is set: this updates, it does not insert
    assert await Note.filter(title="c").acount() == 1
    assert (await Note.aget(id=note.id)).stars == 9


async def check_opt_out_still_validates() -> None:
    with pytest.raises(ModelValidationError):
        await Note.acreate(title="x" * 41, returning=False)
    note = await Note.acreate(title="d", returning=False)
    with pytest.raises(ModelValidationError):
        await Note.aupdate(id=note.id, title="x" * 41, returning=False)
    assert (await Note.aget(id=note.id)).title == "d"


@needs_database
async def test_postgres_default_returns_the_stored_rows(
    postgres: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    await check_default_reads_the_stored_rows(postgres, monkeypatch)


@needs_database
async def test_postgres_opt_out_sends_no_returning_and_counts_rows(
    postgres: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    await check_opt_out_reads_nothing_back(postgres, monkeypatch)
    await check_opt_out_instance_saves_as_an_update()
    await check_opt_out_still_validates()


@needs_database
async def test_postgres_opt_out_does_not_see_database_side_changes(
    postgres: Database,
) -> None:
    await postgres.execute(
        "CREATE FUNCTION ret_notes_upper() RETURNS trigger AS $$ BEGIN "
        "NEW.title := upper(NEW.title); RETURN NEW; END $$ LANGUAGE plpgsql"
    )
    await postgres.execute(
        f"CREATE TRIGGER ret_notes_upper BEFORE INSERT OR UPDATE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION ret_notes_upper()"
    )
    read = await Note.acreate(title="read")
    sent = await Note.acreate(title="sent", returning=False)
    assert (read.title, sent.title) == ("READ", "sent")
    assert (await Note.aget(id=sent.id)).title == "SENT"


async def test_sqlite_default_returns_the_stored_rows(
    sqlite: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    await check_default_reads_the_stored_rows(sqlite, monkeypatch)


async def test_sqlite_opt_out_sends_no_returning_and_counts_rows(
    sqlite: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    await check_opt_out_reads_nothing_back(sqlite, monkeypatch)
    await check_opt_out_instance_saves_as_an_update()
    await check_opt_out_still_validates()


class MySQLDouble:
    """Records statements in place of a MySQL connection (no server here)."""

    dialect = "mysql"

    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row
        self.calls: list[str] = []

    async def execute(self, sql: str, *params: Any) -> int:
        self.calls.append(f"execute {sql}")
        return 1

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append(f"fetch {sql}")
        return [self.row]

    def active(self) -> None:
        return None

    @asynccontextmanager
    async def atomic(self) -> AsyncIterator[None]:
        self.calls.append("atomic")
        yield


@pytest.fixture
def mysql() -> Iterator[MySQLDouble]:
    note = Note(title="m")
    row = {c: getattr(note, c) for c in Note.__columns__}
    double = MySQLDouble(row)
    with use_database(cast(Database, double)):
        yield double


async def test_mysql_default_still_selects_the_written_rows(
    mysql: MySQLDouble,
) -> None:
    await Note.acreate(title="m")
    await Note.aupdate(id=mysql.row["id"], stars=1)
    kinds = [call.split(" ")[:2] for call in mysql.calls]
    assert kinds == [
        ["atomic"],
        ["execute", "INSERT"],
        ["fetch", "SELECT"],
        ["atomic"],
        ["execute", "UPDATE"],
        ["fetch", "SELECT"],
    ]


async def test_mysql_opt_out_skips_the_select(mysql: MySQLDouble) -> None:
    await Note.acreate(title="m", returning=False)
    assert await Note.aupdate(id=mysql.row["id"], stars=1, returning=False) == 1
    count = await Note.filter(title="m").aupdate({"stars": 2}, returning=False)
    assert count == 1
    assert [call.split(" ")[:2] for call in mysql.calls] == [
        ["execute", "INSERT"],
        ["execute", "UPDATE"],
        ["execute", "UPDATE"],
    ]
    assert all("RETURNING" not in call for call in mysql.calls)


def test_a_field_named_like_a_write_option_is_refused() -> None:
    with pytest.raises(RegistrationError, match=r"\['returning'\].*rename the field"):

        class Clash(Model, table="ret_clash"):
            id: UUID = field(primary_key=True, default_factory=uuid4)
            returning: bool = False


@http.post("/notes/sync")
def sync_writes(request: Request) -> dict[str, Any]:
    sent = Note.create(title="s", returning=False)
    read = Note.create(title="s")
    return {
        "sent": sent.title,
        "read": read.title,
        "update": Note.update(id=sent.id, stars=1, returning=False),
        "updated": Note.update(id=read.id, stars=2).stars,
        "where": Note.update_where({"title": "s"}, {"stars": 3}, returning=False),
        "rows": len(Note.update_where({"title": "s"}, {"stars": 4})),
        "query": Note.filter(title="s").update({"stars": 5}, returning=False),
        "listed": [n.stars for n in Note.filter(title="s").update({"stars": 6})],
    }


@pytest.fixture
async def client(pool: Pool, postgres: Database) -> Client:
    assert URL is not None
    app = Hinoki()
    app.include(sync_writes)
    app.install(DatabaseModule(URL, max_size=2))
    return await pool.start(app)


@needs_database
async def test_sync_twins_take_the_same_option(client: Client) -> None:
    reply = await client.request("POST", "/notes/sync")
    assert reply.status == 200
    assert reply.body == {
        "sent": "s",
        "read": "s",
        "update": 1,
        "updated": 2,
        "where": 2,
        "rows": 2,
        "query": 2,
        "listed": [6, 6],
    }
