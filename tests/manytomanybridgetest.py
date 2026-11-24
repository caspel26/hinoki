"""Adopted bridge models, bridge renames and hostile keys in a link scope."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import ClassVar
from uuid import UUID, uuid4

import pytest

from hinoki import db
from hinoki.db import Database
from hinoki.errors import (
    FieldError,
    ModelValidationError,
    RegistrationError,
    UnknownLinkTarget,
)
from hinoki.orm import (
    ManyToMany,
    Model,
    OnDelete,
    field,
    foreign_key,
    many_to_many,
    migrator,
    use_database,
)
from hinoki.orm.links import PENDING, check_resolved
from hinoki.orm.migrations import plan, state_of


class Label(Model, table="m2mb_labels"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str


class Note(Model, table="m2mb_notes"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    labels: ClassVar[ManyToMany[Label]] = many_to_many(Label, bridge="NoteLabel")


def cascade(model: type[Model]) -> UUID:
    reference: UUID = foreign_key(model, on_delete=OnDelete.CASCADE)
    return reference


class NoteLabel(Model, table="m2mb_note_labels", unique=[("note_id", "label_id")]):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    note_id: UUID = cascade(Note)
    label_id: UUID = cascade(Label)
    added_by: str | None = None


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    connection = await Database.connect(
        f"sqlite://{tmp_path / 'b.sqlite3'}", max_size=1, wait=0.5
    )
    plan([Label, Note, NoteLabel], tmp_path, "initial", dialect="sqlite")
    await migrator.apply(connection, tmp_path)
    with use_database(connection):
        yield connection
    connection.close()


def test_a_model_of_the_module_is_adopted_as_the_bridge() -> None:
    side = Note.__links__["labels"]
    assert side.bridge is NoteLabel
    assert Note.__bridges__ == ()


async def test_an_adopted_bridge_keeps_its_extra_columns(database: Database) -> None:
    note = await Note.acreate()
    label = await Label.acreate(name="x")
    await note.labels.aadd(label)
    (link,) = await NoteLabel.filter(note_id=note.id)
    assert link.label_id == label.id and link.added_by is None


def declare_owner(bridge: str, table: str) -> type[Model]:
    class Owner(Model, table=table):
        id: UUID = field(primary_key=True, default_factory=uuid4)
        labels: ClassVar[ManyToMany[Label]] = many_to_many(Label, bridge=bridge)

    return Owner


def test_a_bridge_must_be_unique_and_nullable() -> None:
    first = declare_owner("Loose", "m2mb_owner_loose")
    second = declare_owner("Required", "m2mb_owner_required")
    with pytest.raises(RegistrationError, match="unique"):

        class Loose(Model, table="m2mb_loose"):
            id: UUID = field(primary_key=True, default_factory=uuid4)
            note_id: UUID = cascade(first)
            label_id: UUID = cascade(Label)

    with pytest.raises(RegistrationError, match="nullable"):

        class Required(Model, table="m2mb_required", unique=[("note_id", "label_id")]):
            id: UUID = field(primary_key=True, default_factory=uuid4)
            note_id: UUID = cascade(second)
            label_id: UUID = cascade(Label)
            reason: str


def test_a_bridge_that_is_not_defined_yet_leaves_the_link_pending() -> None:
    class Lost(Model, table="m2mb_lost"):
        id: UUID = field(primary_key=True, default_factory=uuid4)
        labels: ClassVar[ManyToMany[Label]] = many_to_many(Label, bridge="NoSuchBridge")

    try:
        assert Lost.__links__ == {}
        with pytest.raises(RegistrationError, match=r"Lost\.labels .*NoSuchBridge"):
            check_resolved()
        with pytest.raises(RegistrationError, match="never defined"):
            state_of([Label])
    finally:
        PENDING.pop((__name__, "NoSuchBridge"), None)
    check_resolved()


def test_renaming_the_owner_table_renames_the_generated_bridge() -> None:
    @db.table("m2mb_renamed")
    class Owner(Model):
        id: UUID = field(primary_key=True, default_factory=uuid4)
        labels: ClassVar[ManyToMany[Label]] = many_to_many(Label)

    (bridge,) = Owner.__bridges__
    assert Owner.__table__ == "m2mb_renamed"
    assert bridge.__table__ == "m2mb_renamed_labels"


async def test_hostile_keys_in_a_scope_are_inert_data(database: Database) -> None:
    note = await Note.acreate()
    label = await Label.acreate(name="x")
    hostile = "x'); DROP TABLE m2mb_labels;--"
    scope = Label.filter(name=hostile)
    with pytest.raises(UnknownLinkTarget):
        await note.labels.aadd(label, within=scope)
    assert await Label.filter().acount() == 1
    with pytest.raises(ModelValidationError):
        await note.labels.aadd(hostile)
    assert await Label.filter().acount() == 1


async def test_a_scope_with_a_limit_is_refused(database: Database) -> None:
    note = await Note.acreate()
    label = await Label.acreate(name="x")
    with pytest.raises(FieldError):
        await note.labels.aadd(label, within=Label.filter().limit(1))
