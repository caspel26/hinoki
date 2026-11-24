"""Soft deletion, status tracking, and model lifecycle helpers."""

import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid1

import pytest

from hinoki import db, events
from hinoki.db import Database
from hinoki.errors import (
    FieldError,
    ModelError,
    ModelValidationError,
    RegistrationError,
    RelationNotLoaded,
)
from hinoki.fields import UNSET, Field
from hinoki.orm import (
    Model,
    OnDelete,
    SoftDeleteModel,
    TimestampedModel,
    UUIDModel,
    field,
    foreign_key,
    pk,
    urlsafe_token,
    use_database,
    uuid7,
    uuid_pk,
)
from hinoki.orm.migrations import table_state

NOW = datetime(2026, 11, 24, tzinfo=UTC)
CURRENT_TIME = [NOW]


def clock() -> datetime:
    return CURRENT_TIME[0]


@db.timestamps(clock=clock)
@db.track_status("state", changed_at="state_changed_at", clock=clock)
@db.track_status("priority", changed_at="priority_changed_at", clock=clock)
@db.soft_delete(clock=clock)
class UtilityRecord(Model, table="utility_records"):
    id: int = pk()
    name: str
    state: str = "new"
    priority: str = "normal"
    updated_at: datetime = field(default_factory=clock)
    state_changed_at: datetime = field(default_factory=clock)
    priority_changed_at: datetime = field(default_factory=clock)
    deleted_at: datetime | None = None


@db.soft_delete(clock=clock)
class UtilityParent(Model, table="utility_parents"):
    id: int = pk()
    name: str
    deleted_at: datetime | None = None


@db.soft_delete(clock=clock)
class UtilityChild(Model, table="utility_children"):
    id: int = pk()
    parent_id: int = foreign_key(
        UtilityParent, on_delete=OnDelete.RESTRICT, related_name="children"
    )
    deleted_at: datetime | None = None


class CreatedBase(TimestampedModel, SoftDeleteModel, abstract=True):
    """Exercise composed abstract lifecycle columns."""

    label: str = ""


@db.track_status("state", changed_at="state_changed_at")
class StateTrackBase(Model, abstract=True):
    state: str = ""
    state_changed_at: datetime = field(default_factory=clock)


@db.track_status("phase", changed_at="phase_changed_at")
class PhaseTrackBase(Model, abstract=True):
    phase: str = ""
    phase_changed_at: datetime = field(default_factory=clock)


class ComposedRecord(CreatedBase, table="composed_utility_records"):
    id: int = pk()


class MultiTrackRecord(StateTrackBase, PhaseTrackBase, table="multi_track_records"):
    id: int = pk()


@db.soft_delete("archived_at")
class DeleteOverride(SoftDeleteModel, table="utility_delete_override"):
    id: int = pk()
    archived_at: datetime | None = None


@db.track_status("phase", changed_at="phase_changed_at")
class TrackOverride(StateTrackBase, table="utility_track_override"):
    id: int = pk()
    phase: str = ""
    phase_changed_at: datetime = field(default_factory=clock)


def alternate_clock() -> datetime:
    return NOW


@db.track_status("state", changed_at="state_override_changed_at", clock=alternate_clock)
class TrackClockOverride(StateTrackBase, table="utility_track_clock_override"):
    id: int = pk()
    state_override_changed_at: datetime = field(default_factory=clock)


@db.soft_delete()
class FailingLifecycle(Model, table="utility_failing_lifecycle"):
    id: int = pk()
    fail_in: str = ""
    deleted_at: datetime | None = None

    async def before_save(self) -> None:
        if self.fail_in:
            raise RuntimeError(self.fail_in)


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    opened = await Database.connect(f"sqlite://{tmp_path / 'model-utilities.sqlite3'}")
    await opened.execute(
        "CREATE TABLE utility_records (id integer PRIMARY KEY, name text NOT NULL, "
        "state text NOT NULL, priority text NOT NULL, updated_at text NOT NULL, "
        "state_changed_at text NOT NULL, priority_changed_at text NOT NULL, "
        "deleted_at text)"
    )
    await opened.execute(
        "CREATE TABLE utility_parents (id integer PRIMARY KEY, name text NOT NULL, "
        "deleted_at text)"
    )
    await opened.execute(
        "CREATE TABLE utility_children (id integer PRIMARY KEY, "
        "parent_id integer NOT NULL, "
        "deleted_at text, FOREIGN KEY(parent_id) REFERENCES utility_parents(id))"
    )
    await opened.execute(
        "CREATE TABLE utility_failing_lifecycle (id integer PRIMARY KEY, "
        "fail_in text NOT NULL, deleted_at text)"
    )
    try:
        with use_database(opened):
            yield opened
    finally:
        opened.close()


def test_abstract_lifecycle_bases_compose_and_migrate_as_columns() -> None:
    assert ComposedRecord.__updated_at__ == "updated_at"
    assert ComposedRecord.__soft_delete__ == "deleted_at"
    state = table_state(ComposedRecord, "sqlite")
    assert {"created_at", "updated_at", "deleted_at"} <= set(state["columns"])
    decorated = table_state(UtilityRecord, "sqlite")
    assert {"deleted_at", "state_changed_at", "priority_changed_at"} <= set(
        decorated["columns"]
    )
    assert "state_changed_at" in decorated["columns"]
    assert {track.field for track in MultiTrackRecord.__status_tracks__} == {
        "state",
        "phase",
    }
    assert DeleteOverride.__soft_delete__ == "archived_at"
    assert [track.field for track in TrackOverride.__status_tracks__] == ["phase"]
    assert [track.field for track in TrackClockOverride.__status_tracks__] == ["state"]
    assert TrackClockOverride.__status_tracks__[0].clock is alternate_clock


def define_conflicting_inherited_managed_columns() -> None:
    @db.track_status("status", changed_at="updated_at")
    class ConflictingStatusBase(Model, abstract=True):
        status: str
        updated_at: datetime

    class ConflictingInheritedColumns(
        TimestampedModel, ConflictingStatusBase, table="inherited_column_collision"
    ):
        id: int = pk()


def test_registration_rejects_invalid_soft_delete_and_status_tracks() -> None:
    with pytest.raises(RegistrationError, match="unknown"):

        @db.soft_delete("missing")
        class MissingColumn(Model, table="missing_soft_delete_column"):
            id: int = pk()

    with pytest.raises(RegistrationError, match="datetime"):

        @db.soft_delete()
        class WrongType(Model, table="wrong_soft_delete_type"):
            id: int = pk()
            deleted_at: str = ""

    with pytest.raises(RegistrationError, match="distinct"):

        @db.track_status("state", changed_at="state")
        class SameField(Model, table="same_status_field"):
            id: int = pk()
            state: datetime

    with pytest.raises(RegistrationError, match="clock"):

        @db.track_status(clock=42)  # type: ignore[arg-type]
        class BadClock(Model, table="bad_status_clock"):
            id: int = pk()
            status: str
            status_changed_at: datetime

    with pytest.raises(RegistrationError, match="conflicting"):

        @db.track_status("state", changed_at="state_changed_at")
        @db.track_status("state", changed_at="other_changed_at")
        class DuplicateTrack(Model, table="duplicate_status_track"):
            id: int = pk()
            state: str
            state_changed_at: datetime
            other_changed_at: datetime

    with pytest.raises(RegistrationError, match="conflicting"):

        @db.track_status("state", changed_at="updated_at")
        @db.timestamps()
        class TimestampCollision(Model, table="track_timestamp_collision"):
            id: int = pk()
            state: str
            updated_at: datetime

    with pytest.raises(RegistrationError, match="conflicts"):

        @db.timestamps("updated_at")
        @db.track_status("status", changed_at="updated_at")
        class TimestampTrackCollision(Model, table="timestamp_track_collision"):
            id: int = pk()
            status: str
            updated_at: datetime

    with pytest.raises(RegistrationError, match="inherited managed"):
        define_conflicting_inherited_managed_columns()


async def test_soft_delete_scopes_restore_hard_delete_and_status_tracking(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys.modules[__name__], "CURRENT_TIME", [NOW])
    made = await UtilityRecord.acreate(id=1, name="a")
    assert made.previous("name") == "a"
    assert not made.changed_fields
    fresh = UtilityRecord(id=8, name="fresh")
    assert fresh.previous("name") is UNSET
    with pytest.raises(FieldError, match="no field"):
        made.previous("missing")

    CURRENT_TIME[0] = datetime(2026, 11, 25, tzinfo=UTC)
    made.state = "paid"
    made.priority = "urgent"
    assert set(made.changed_fields) == {"state", "priority"}
    await made.asave()
    assert str(made.state_changed_at).startswith("2026-11-25")
    assert str(made.priority_changed_at).startswith("2026-11-25")
    assert made.previous("state") == "paid"
    assert not made.changed_fields

    CURRENT_TIME[0] = datetime(2026, 11, 26, tzinfo=UTC)
    await UtilityRecord.filter(id=1).aupdate({"state": "shipped"})
    assert str((await UtilityRecord.aget(id=1)).state_changed_at).startswith(
        "2026-11-26"
    )
    assert await UtilityRecord.all().acount() == 1

    await made.asoft_delete()
    assert made.is_deleted
    assert await UtilityRecord.all().acount() == 0
    assert await UtilityRecord.with_deleted().acount() == 1
    assert await UtilityRecord.only_deleted().acount() == 1
    assert await UtilityRecord.filter(id=1).adelete() == 0
    assert await UtilityRecord.only_deleted().filter(id=1).arestore() == 1
    assert not (await UtilityRecord.aget(id=1)).is_deleted
    assert await UtilityRecord.filter(id=1).adelete() == 1
    deleted_row = await UtilityRecord.with_deleted().filter(id=1).aget()
    assert await deleted_row.ahard_delete() == 1
    assert await UtilityRecord.with_deleted().acount() == 0


async def test_soft_delete_writes_require_explicit_filters(database: Database) -> None:
    with pytest.raises(FieldError, match="adelete needs at least one condition"):
        await UtilityRecord.all().adelete()
    with pytest.raises(FieldError, match="explicit condition"):
        await UtilityRecord.all().ahard_delete()
    with pytest.raises(FieldError, match="aupdate needs filters and values"):
        await UtilityRecord.all().aupdate({"name": "wipe"})
    with pytest.raises(FieldError, match="explicit condition"):
        await UtilityRecord.only_deleted().arestore()
    with pytest.raises(FieldError, match="explicit condition"):
        await UtilityRecord.with_deleted().ahard_delete()


async def test_lifecycle_hook_failures_restore_marker_and_allow_retry(
    database: Database,
) -> None:
    row = await FailingLifecycle.acreate(id=1)
    row.fail_in = "soft-delete"
    with pytest.raises(RuntimeError, match="soft-delete"):
        await row.asoft_delete()
    assert row.deleted_at is None
    row.fail_in = ""
    await row.asoft_delete()
    assert row.is_deleted

    row.fail_in = "restore"
    with pytest.raises(RuntimeError, match="restore"):
        await row.arestore()
    assert row.is_deleted
    row.fail_in = ""
    await row.arestore()
    assert not row.is_deleted


async def test_instance_delete_restore_are_idempotent_and_unsaved_is_refused(
    database: Database,
) -> None:
    fresh = UtilityRecord(id=2, name="fresh")
    with pytest.raises(FieldError, match="unsaved"):
        await fresh.asoft_delete()
    saved = await fresh.insert()
    await saved.asoft_delete()
    await saved.asoft_delete()
    await saved.arestore()
    await saved.arestore()
    assert not saved.is_deleted


async def test_relations_hide_deleted_children_and_a_deleted_forward_parent(
    database: Database,
) -> None:
    parent = await UtilityParent.acreate(id=1, name="parent")
    live = await UtilityChild.acreate(id=1, parent_id=parent.id)
    deleted = await UtilityChild.acreate(id=2, parent_id=parent.id)
    await deleted.asoft_delete()
    await parent.asoft_delete()

    loaded_parent = (
        await UtilityParent.with_deleted()
        .prefetch_related("children")
        .filter(id=parent.id)
        .aget()
    )
    assert loaded_parent.is_deleted
    assert loaded_parent.related("children") == [live]

    loaded_child = (
        await UtilityChild.all().select_related("parent").filter(id=live.id).aget()
    )
    assert loaded_child.related("parent") is None
    (prefetched,) = await UtilityChild.all().prefetch_related("parent").filter(id=1)
    assert prefetched.related("parent") is None
    assert not await UtilityChild.all().filter(parent__name="parent").aexists()
    prefetched.parent_id = 2
    with pytest.raises(RelationNotLoaded, match="loaded for another"):
        prefetched.related("parent")


async def test_status_timestamp_only_changes_when_value_changes_and_allows_override(
    database: Database,
) -> None:
    row = await UtilityRecord.acreate(id=21, name="unchanged")
    original = row.state_changed_at
    await row.asave()
    assert row.state_changed_at == original

    override = datetime(2026, 11, 20, tzinfo=UTC)
    row.state = "changed"
    row.state_changed_at = override
    await row.asave()
    assert str(row.state_changed_at).startswith("2026-11-20")


async def test_soft_delete_entry_points_refuse_models_without_the_option() -> None:
    class PlainRecord(Model, table="plain_utility_record"):
        id: int = pk()

    with pytest.raises(ModelError, match="soft deletion"):
        PlainRecord.with_deleted()
    with pytest.raises(ModelError, match="soft deletion"):
        PlainRecord.only_deleted()
    with pytest.raises(ModelError, match="soft deletion"):
        await PlainRecord.all().arestore()


async def test_queryset_soft_delete_emits_deleted_count(database: Database) -> None:
    seen: list[events.Deleted[Any]] = []
    handlers = events.Handlers()

    @handlers.after_commit(events.Deleted)
    async def record(event: events.Deleted[Any]) -> None:
        seen.append(event)

    await UtilityRecord.acreate(id=9, name="event")
    bus = events.EventBus([handlers])
    with events.use(bus):
        assert await UtilityRecord.filter(id=9).adelete() == 1
        assert await UtilityRecord.filter(id=9).adelete() == 0
    assert [(event.subject, event.count) for event in seen] == [(UtilityRecord, 1)]


async def test_uuid_model_uuid_pk_and_urlsafe_token_work_end_to_end(
    tmp_path: Path,
) -> None:
    class ShareLink(UUIDModel, TimestampedModel, table="utility_share_links"):
        token: str = urlsafe_token()
        short_code: str = urlsafe_token(nbytes=12)

    class OrderedEvent(Model, table="utility_ordered_events"):
        id: UUID = uuid_pk(version=7)
        name: str

    class CustomUuidRow(Model, table="utility_custom_uuids"):
        id: UUID = uuid_pk(default_factory=uuid1)

    generated_v7 = uuid7()
    assert generated_v7.version == 7
    assert generated_v7 != uuid7()

    link = ShareLink()
    assert isinstance(link.id, UUID)
    assert link.id.version == 4
    assert len(link.token) == 43
    assert len(link.short_code) == 16

    event = OrderedEvent(name="created")
    assert event.id.version == 7
    assert CustomUuidRow().id.version == 1

    state = table_state(ShareLink)
    assert state["columns"]["id"]["primary_key"] is True
    assert state["columns"]["token"]["unique"] is True
    assert state["columns"]["token"]["type"] == "varchar(43)"
    assert state["columns"]["short_code"]["type"] == "varchar(16)"

    database = await Database.connect(f"sqlite://{tmp_path / 'tokens.sqlite3'}")
    try:
        await database.execute(
            "CREATE TABLE utility_share_links ("
            "id TEXT PRIMARY KEY, "
            "created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, "
            "token TEXT NOT NULL UNIQUE, "
            "short_code TEXT NOT NULL UNIQUE)"
        )
        with use_database(database):
            saved = await ShareLink().insert()
            fetched = await ShareLink.aget(id=saved.id)
            assert fetched.token == saved.token
            assert fetched.short_code == saved.short_code
            with pytest.raises(ModelValidationError):
                await ShareLink(short_code="not url safe!").insert()
    finally:
        database.close()


def test_uuid_pk_and_urlsafe_token_reject_invalid_options() -> None:
    with pytest.raises(RegistrationError, match="not both"):
        uuid_pk(version=7, default_factory=uuid1)
    with pytest.raises(RegistrationError, match="callable"):
        uuid_pk(default_factory="not-callable")  # type: ignore[arg-type]
    with pytest.raises(RegistrationError, match="4 or 7"):
        uuid_pk(version=3)  # type: ignore[arg-type]

    with pytest.raises(RegistrationError, match="between 1 and 1024"):
        urlsafe_token(nbytes=0)
    with pytest.raises(RegistrationError, match="between 1 and 1024"):
        urlsafe_token(nbytes=1025)
    with pytest.raises(RegistrationError, match="at least 43"):
        urlsafe_token(nbytes=32, max_length=16)
    with pytest.raises(RegistrationError, match="URL-safe character pattern"):
        urlsafe_token(constraints=Field(pattern="^[0-9]+$"))

    # Explicit constraints.max_length larger than generated length is accepted.
    class WideToken(Model, table="utility_wide_tokens"):
        id: int = pk()
        code: str = urlsafe_token(nbytes=16, constraints=Field(max_length=64))

    assert table_state(WideToken)["columns"]["code"]["type"] == "varchar(64)"
