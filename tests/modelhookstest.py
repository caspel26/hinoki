"""Model lifecycle hooks, instance state and ``after_commit``."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from hinoki.admin.modeladmin import ModelAdmin
from hinoki.auth import Caller
from hinoki.db import Database, DatabaseError
from hinoki.errors import FieldError
from hinoki.fields import UNSET
from hinoki.orm import Change, Model, pk, use_database

LOG: list[str] = []
COMMITTED: list[str] = []


class Entry(Model, table="hook_entries"):
    id: int = pk()
    title: str
    slug: str = ""
    fail_in: str = ""

    async def before_save(self) -> None:
        LOG.append(f"before_save new={self.is_new}")
        if self.was_changed("title"):
            self.slug = self.title.lower().replace(" ", "-")
        if self.fail_in == "before_save":
            raise RuntimeError("before_save")

    def before_create(self) -> None:  # a sync hook
        LOG.append("before_create")

    async def after_create(self) -> None:
        LOG.append(f"after_create new={self.is_new}")
        self.after_commit(lambda: COMMITTED.append(f"created {self.title}"))
        if self.fail_in == "after_create":
            raise RuntimeError("after_create")

    async def after_save(self) -> None:
        LOG.append(f"after_save changes={sorted(self.changes)}")

        async def announce() -> None:
            COMMITTED.append(f"saved {self.title}")

        self.after_commit(announce)
        if self.fail_in == "after_save":
            raise RuntimeError("after_save")


class Plain(Model, table="hook_plain"):
    id: int = pk()
    title: str


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    LOG.clear()
    COMMITTED.clear()
    connected = await Database.connect(f"sqlite://{tmp_path / 'hooks.sqlite3'}")
    for table in ("hook_entries", "hook_plain"):
        extra = (
            ", slug text NOT NULL, fail_in text NOT NULL" if "entries" in table else ""
        )
        await connected.execute(
            f"CREATE TABLE {table} (id integer PRIMARY KEY, title text NOT NULL{extra})"
        )
    with use_database(connected):
        yield connected
    connected.close()


async def rows(database: Database) -> list[dict[str, Any]]:
    return [dict(row) for row in await database.fetch("SELECT * FROM hook_entries")]


def test_only_overridden_hooks_are_recorded() -> None:
    assert Entry.__write_hooks__ == (
        "before_save",
        "before_create",
        "after_create",
        "after_save",
    )
    assert Plain.__write_hooks__ == ()


async def test_create_runs_hooks_in_order_with_state(database: Database) -> None:
    entry = await Entry.acreate(id=1, title="Hello World")

    assert LOG == [
        "before_save new=True",
        "before_create",
        "after_create new=True",
        "after_save changes=['fail_in', 'id', 'slug', 'title']",
    ]
    assert entry.slug == "hello-world"
    assert not entry.is_new
    assert not entry.is_dirty
    assert (await rows(database))[0]["slug"] == "hello-world"
    assert COMMITTED == ["created Hello World", "saved Hello World"]


async def test_update_runs_save_hooks_only_and_exposes_changes(
    database: Database,
) -> None:
    entry = await Entry.acreate(id=1, title="One")
    LOG.clear()
    COMMITTED.clear()

    entry.title = "Two"
    assert entry.is_dirty
    assert entry.was_changed("title")
    assert not entry.was_changed("fail_in")
    assert entry.changes == {"title": Change(old="One", new="Two")}
    await entry.asave()

    assert LOG == ["before_save new=False", "after_save changes=['slug', 'title']"]
    assert entry.slug == "two"
    assert COMMITTED == ["saved Two"]
    assert not entry.is_dirty


async def test_save_without_changes_skips_the_write_and_after_hooks(
    database: Database,
) -> None:
    entry = await Entry.acreate(id=1, title="One")
    LOG.clear()
    COMMITTED.clear()

    await entry.asave()

    assert LOG == ["before_save new=False"]
    assert COMMITTED == []


async def test_new_instance_reports_every_column_as_new(database: Database) -> None:
    entry = Entry(id=1, title="x")

    assert entry.is_new
    assert entry.is_dirty
    assert entry.changes["title"] == Change(old=UNSET, new="x")
    assert entry.was_changed("title")
    with pytest.raises(FieldError, match="no column 'nope'"):
        entry.was_changed("nope")


@pytest.mark.parametrize("stage", ["before_save", "after_create", "after_save"])
async def test_a_failing_hook_rolls_the_write_back(
    database: Database, stage: str
) -> None:
    with pytest.raises(RuntimeError, match=stage):
        await Entry.acreate(id=1, title="Boom", fail_in=stage)

    assert await rows(database) == []
    assert COMMITTED == []


async def test_a_failing_update_hook_rolls_back_and_drops_callbacks(
    database: Database,
) -> None:
    entry = await Entry.acreate(id=1, title="One")
    COMMITTED.clear()
    entry.title = "Two"
    entry.fail_in = "after_save"

    with pytest.raises(RuntimeError, match="after_save"):
        await entry.asave()

    assert (await rows(database))[0]["title"] == "One"
    assert COMMITTED == []


async def test_callbacks_wait_for_the_outer_transaction(database: Database) -> None:
    async with database.transaction():
        await Entry.acreate(id=1, title="A")
        assert COMMITTED == []
        await Entry.acreate(id=2, title="B")
    assert COMMITTED == ["created A", "saved A", "created B", "saved B"]


async def test_an_outer_rollback_drops_callbacks(database: Database) -> None:
    async def write_then_fail() -> None:
        async with database.transaction():
            await Entry.acreate(id=1, title="A")
            raise RuntimeError("outer")

    with pytest.raises(RuntimeError, match="outer"):
        await write_then_fail()

    assert COMMITTED == []
    assert await rows(database) == []


async def test_a_rolled_back_savepoint_drops_only_its_callbacks(
    database: Database,
) -> None:
    async with database.transaction():
        await Entry.acreate(id=1, title="Kept")
        with pytest.raises(RuntimeError, match="after_save"):
            await Entry.acreate(id=2, title="Lost", fail_in="after_save")

    assert COMMITTED == ["created Kept", "saved Kept"]
    assert [row["title"] for row in await rows(database)] == ["Kept"]


async def test_a_failing_commit_callback_is_logged_not_raised(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    def fail() -> None:
        raise RuntimeError("callback")

    async with database.transaction():
        database.after_commit(fail)
        database.after_commit(lambda: COMMITTED.append("second"))

    assert COMMITTED == ["second"]
    assert "an after_commit callback failed" in caplog.text


async def test_after_commit_outside_a_transaction_is_an_error(
    database: Database,
) -> None:
    with pytest.raises(DatabaseError, match="needs an active transaction"):
        database.after_commit(lambda: None)


async def test_models_without_hooks_write_without_a_transaction(
    database: Database,
) -> None:
    plain = await Plain.acreate(id=1, title="x")
    plain.title = "y"
    await plain.asave()

    assert database.active() is None
    assert [dict(r) for r in await database.fetch("SELECT * FROM hook_plain")] == [
        {"id": 1, "title": "y"}
    ]


async def test_bulk_updates_run_no_hooks(database: Database) -> None:
    await Entry.acreate(id=1, title="One")
    LOG.clear()

    await Entry.filter(id=1).aupdate({"title": "Bulk"})

    assert LOG == []


async def test_the_admin_save_runs_the_hooks_on_create_and_update(
    database: Database,
) -> None:
    class EntryAdmin(ModelAdmin[Entry]):
        pass

    admin = EntryAdmin()
    admin.model = Entry
    principal = Caller(id="admin", user="admin")

    created = await admin.save({"id": 1, "title": "Made In Admin"}, principal)
    assert created.slug == "made-in-admin"
    LOG.clear()

    saved = await admin.save({"title": "Edited"}, principal, created)

    assert saved.slug == "edited"
    assert LOG == ["before_save new=False", "after_save changes=['slug', 'title']"]
    assert (await rows(database))[0]["slug"] == "edited"
