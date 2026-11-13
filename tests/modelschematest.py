"""Models take schemas: from_schema, acreate(data, ...), assign, aupdate_from."""

import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest

from hinoki import UNSET, Schema, Unset
from hinoki.db import Database
from hinoki.errors import FieldError
from hinoki.orm import Model, field, use_database

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
needs_database = pytest.mark.skipif(URL is None, reason="no database")


class Entry(Model, table="ms_entries"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    owner: str = field(max_length=20)
    title: str = field(max_length=40)
    body: str = ""


class EntryIn(Schema):
    title: str
    body: str = ""
    owner: str = "client-chosen"  # a schema that (wrongly) lets the client send it


class EntryPatch(Schema):
    title: str | Unset = UNSET
    body: str | Unset = UNSET


def test_from_schema_merges_with_explicit_values_winning() -> None:
    entry = Entry.from_schema(EntryIn(title="t"), owner="server")
    assert (entry.title, entry.body, entry.owner) == ("t", "", "server")
    assert Entry.from_schema(owner="o", title="x").title == "x"
    with pytest.raises(FieldError):
        Entry.from_schema(EntryIn(title="t"), nonsense=1)


def test_assign_touches_only_the_fields_that_were_sent() -> None:
    entry = Entry(owner="o", title="old", body="keep")
    entry.assign(EntryPatch(title="new"))
    assert (entry.title, entry.body) == ("new", "keep")
    entry.assign(EntryPatch(), body="x")
    assert entry.body == "x"
    with pytest.raises(FieldError):
        entry.assign(nonsense=1)


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    assert URL is not None
    db = await Database.connect(URL, max_size=2)
    await db.execute("DROP TABLE IF EXISTS ms_entries")
    await db.execute(
        "CREATE TABLE ms_entries (id uuid PRIMARY KEY, owner varchar(20) NOT NULL, "
        "title varchar(40) NOT NULL, body text NOT NULL)"
    )
    with use_database(db):
        yield db
    await db.execute("DROP TABLE IF EXISTS ms_entries")
    db.close()


@needs_database
async def test_acreate_takes_a_schema_and_server_values_win(database: Database) -> None:
    made = await Entry.acreate(EntryIn(title="t", body="b"), owner="server")
    stored = (await database.fetch("SELECT * FROM ms_entries"))[0]
    assert (stored["owner"], stored["title"], stored["body"]) == ("server", "t", "b")
    assert made.owner == "server"
    plain = await Entry.acreate(owner="o", title="plain")
    assert plain.title == "plain"


@needs_database
async def test_aupdate_from_writes_only_what_changed(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await Entry.acreate(owner="o", title="old", body="keep")
    loaded = await Entry.aget(id=made.id)
    seen: list[str] = []
    original = Database.fetch

    async def counting(self: Database, sql: str, *params: Any) -> Any:
        seen.append(sql)
        return await original(self, sql, *params)

    monkeypatch.setattr(Database, "fetch", counting)
    await loaded.aupdate_from(EntryPatch(title="new"))
    assert len(seen) == 1 and seen[0].startswith("UPDATE")
    assert '"body"' not in seen[0].split("SET")[1].split("WHERE")[0]
    seen.clear()
    await loaded.aupdate_from(EntryPatch())  # nothing sent, nothing written
    assert seen == []
    row = (await database.fetch("SELECT * FROM ms_entries"))[0]
    assert (row["title"], row["body"]) == ("new", "keep")
