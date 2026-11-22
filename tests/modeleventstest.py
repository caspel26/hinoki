"""Model events: Created, Updated and Deleted from the model write paths."""

import itertools
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from hinoki import API, Hinoki, Schema, Unset, crud, events
from hinoki.db import Database, DatabaseModule
from hinoki.errors import EventError
from hinoki.events import Created, Deleted, Updated
from hinoki.fields import UNSET
from hinoki.orm import Model, pk, use_database
from hinoki.runtime.nativeproof import native_refusal
from hinoki.runtime.pipeline import Pipeline
from hinoki.runtime.servercore import build_server_core
from hinoki.settings import EventsSettings
from hinoki.testing import ASGIClient


class Note(Model, table="event_notes"):
    id: int = pk()
    title: str
    body: str = ""


class Tag(Model, table="event_tags"):
    id: int = pk()
    name: str


TABLES = (
    "CREATE TABLE event_notes "
    "(id integer PRIMARY KEY, title text NOT NULL, body text NOT NULL)",
    "CREATE TABLE event_tags (id integer PRIMARY KEY, name text NOT NULL)",
)


async def create_tables(database: Database) -> None:
    for statement in TABLES:
        await database.execute(statement)


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    opened = await Database.connect(f"sqlite://{tmp_path / 'm.sqlite3'}", max_size=2)
    await create_tables(opened)
    try:
        with use_database(opened):
            yield opened
    finally:
        opened.close()


def bus_of(handlers: events.Handlers, **limits: float) -> events.EventBus:
    return events.EventBus([handlers], EventsSettings(**limits))  # type: ignore[arg-type]


async def test_created_carries_the_stored_instance(database: Database) -> None:
    seen: list[Created[Note]] = []
    handlers = events.Handlers()

    @handlers.after_commit(Created[Note])
    async def record(event: Created[Note]) -> None:
        seen.append(event)

    with events.use(bus_of(handlers)):
        made = await Note.acreate(id=1, title="a")
        await Tag.acreate(id=1, name="other model")
    assert len(seen) == 1
    assert seen[0].subject is Note
    assert seen[0].instance is made
    assert not made.is_dirty


async def test_updated_names_the_changed_columns_and_skips_no_ops(
    database: Database,
) -> None:
    seen: list[Updated[Note]] = []
    handlers = events.Handlers()

    @handlers.after_commit(Updated)
    async def record(event: Updated[Any]) -> None:
        seen.append(event)

    note = await Note.acreate(id=1, title="a")
    with events.use(bus_of(handlers)):
        await note.aupdate_from()  # nothing changed: no write, no event
        await note.aupdate_from(title="b", body="x")
    assert [(e.subject, e.instance, e.changed) for e in seen] == [
        (Note, note, ("title", "body"))
    ]
    assert note.title == "b"


async def test_deleted_counts_the_rows_of_one_statement(database: Database) -> None:
    seen: list[Deleted[Note]] = []
    handlers = events.Handlers()

    @handlers.after_commit(Deleted[Note])
    async def record(event: Deleted[Note]) -> None:
        seen.append(event)

    await Note.abulk_create([{"id": i, "title": "t"} for i in range(1, 5)])
    with events.use(bus_of(handlers)):
        assert await Note.adelete(id=1) == 1
        assert await Note.adelete(id=99) == 0  # nothing deleted: nothing emitted
        assert await Note.abulk_delete([2, 3]) == 2
        await Tag.abulk_delete([1])
    assert [(e.subject, e.count) for e in seen] == [(Note, 1), (Note, 2)]


async def test_bulk_writes_emit_one_event_per_row(database: Database) -> None:
    seen: list[str] = []
    handlers = events.Handlers()

    @handlers.after_commit(Created)
    async def created(event: Created[Any]) -> None:
        seen.append(f"created {event.instance.id}")

    @handlers.after_commit(Updated)
    async def updated(event: Updated[Any]) -> None:
        seen.append(f"updated {event.instance.id} {event.changed}")

    with events.use(bus_of(handlers)):
        rows = await Note.abulk_create(
            [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}]
        )
        rows[0].title = "c"
        rows[1].title = "d"
        await Note.abulk_update(rows)
    assert seen == [
        "created 1",
        "created 2",
        "updated 1 ('title',)",
        "updated 2 ('title',)",
    ]


async def test_a_failing_in_transaction_handler_rolls_the_write_back(
    database: Database,
) -> None:
    handlers = events.Handlers()

    @handlers.in_transaction(Created[Note])
    async def refuse(event: Created[Note]) -> None:
        raise RuntimeError("no create")

    @handlers.in_transaction(Updated[Note])
    async def refuse_update(event: Updated[Note]) -> None:
        raise RuntimeError("no update")

    @handlers.in_transaction(Deleted[Note])
    async def refuse_delete(event: Deleted[Note]) -> None:
        raise RuntimeError("no delete")

    kept = await Note.acreate(id=2, title="kept")
    with events.use(bus_of(handlers)):
        with pytest.raises(RuntimeError, match="no create"):
            await Note.acreate(id=1, title="a")
        kept.title = "changed"
        with pytest.raises(RuntimeError, match="no update"):
            await kept.asave()
        with pytest.raises(RuntimeError, match="no delete"):
            await Note.adelete(id=2)
    stored = await Note.aget(id=2)
    assert stored.title == "kept"
    assert await Note.filter(id=1).acount() == 0


async def test_after_commit_waits_for_the_transaction(database: Database) -> None:
    seen: list[int] = []
    handlers = events.Handlers()

    @handlers.after_commit(Created[Note])
    async def record(event: Created[Note]) -> None:
        seen.append(event.instance.id)

    async def abandoned() -> None:
        async with database.atomic():
            await Note.acreate(id=1, title="rolled back")
            assert seen == []
            raise RuntimeError("abort")

    with events.use(bus_of(handlers)):
        with pytest.raises(RuntimeError):
            await abandoned()
        assert seen == []
        async with database.atomic():
            await Note.acreate(id=2, title="kept")
            assert seen == []
        assert seen == [2]


async def test_a_write_with_no_listener_emits_nothing(database: Database) -> None:
    handlers = events.Handlers()

    @handlers.after_commit(Created[Tag])
    async def record(event: Created[Tag]) -> None:
        raise AssertionError("not for notes")

    bus = bus_of(handlers)
    assert events.listener(Created, Note) is None
    with events.use(bus):
        assert events.listener(Created, Note) is None
        assert events.listener(Created, Tag) is bus
        await Note.acreate(id=1, title="a")
    with bus.capture() as seen, events.use(bus):
        await Note.acreate(id=2, title="b")
    assert [type(e) for e in seen] == [Created]


async def test_handlers_that_write_other_models_stop_at_max_depth(
    database: Database,
) -> None:
    ids = itertools.count(1)
    handlers = events.Handlers()

    @handlers.in_transaction(Created[Note])
    async def tag_it(event: Created[Note]) -> None:
        await Tag.acreate(id=next(ids), name="t")

    @handlers.in_transaction(Created[Tag])
    async def note_it(event: Created[Tag]) -> None:
        await Note.acreate(id=next(ids), title="n")

    with (
        events.use(bus_of(handlers, max_depth=3)),
        pytest.raises(EventError, match="deeper than 3"),
    ):
        await Note.acreate(id=100, title="start")
    assert await Note.filter(id=100).acount() == 0
    assert await Tag.filter(id=1).acount() == 0


class Item(Model, table="event_items"):
    id: int = pk()
    title: str


class ItemOut(Schema):
    id: int
    title: str


class ItemIn(Schema):
    id: int
    title: str


class ItemPatch(Schema):
    title: str | Unset = UNSET


@crud(
    Item,
    path="/items",
    read=ItemOut,
    create=ItemIn,
    update=ItemPatch,
    delete=True,
    public_writes=True,
)
class Items(API):
    pass


def item_handlers(seen: list[str]) -> events.Handlers:
    handlers = events.Handlers()

    @handlers.after_commit(Created[Item])
    async def created(event: Created[Item]) -> None:
        seen.append(f"created {event.instance.title}")

    @handlers.after_commit(Updated[Item])
    async def updated(event: Updated[Item]) -> None:
        seen.append(f"updated {event.instance.title} {event.changed}")

    @handlers.after_commit(Deleted[Item])
    async def deleted(event: Deleted[Item]) -> None:
        seen.append(f"deleted {event.count}")

    return handlers


@pytest.fixture
async def items_url(tmp_path: Path) -> AsyncIterator[str]:
    url = f"sqlite://{tmp_path / 'items.sqlite3'}"
    admin = await Database.connect(url, max_size=1)
    await admin.execute(
        "CREATE TABLE event_items (id integer PRIMARY KEY, title text NOT NULL)"
    )
    yield url
    admin.close()


async def test_generated_crud_writes_emit_the_events(items_url: str) -> None:
    seen: list[str] = []
    app = Hinoki(
        include=[Items],
        modules=[DatabaseModule(items_url, max_size=2)],
        events=[item_handlers(seen)],
    )
    async with ASGIClient(app) as client:
        assert (await client.post("/items", json={"id": 1, "title": "a"})).status == 201
        assert (await client.patch("/items/1", json={"title": "b"})).status == 200
        assert (await client.delete("/items/1")).status == 204
    assert seen == ["created a", "updated b ('title',)", "deleted 1"]


def lane_reasons(app: Hinoki) -> dict[tuple[str, str], str]:
    pipeline = Pipeline(app)
    build_server_core(pipeline)
    return {
        (binding.route.method, binding.route.path): reason
        for binding, reason in zip(
            pipeline.bindings, pipeline.crud_plan_reasons, strict=True
        )
    }


def test_events_move_only_the_matching_writes_off_the_native_lane() -> None:
    modules = [DatabaseModule("sqlite:///events-unused.sqlite3")]
    plain = lane_reasons(Hinoki(include=[Items], modules=modules))
    assert set(plain.values()) == {"eligible"}

    handlers = events.Handlers()

    @handlers.after_commit(Updated[Item])
    async def updated(event: Updated[Item]) -> None: ...

    @handlers.in_transaction(Deleted)
    async def deleted(event: Deleted[Any]) -> None: ...

    listed = lane_reasons(Hinoki(include=[Items], modules=modules, events=[handlers]))
    assert listed[("GET", "/items")] == "eligible"
    assert listed[("POST", "/items")] == "eligible"
    assert listed[("PATCH", "/items/{item_id}")] == "model_event_handlers"
    assert listed[("DELETE", "/items/{item_id}")] == "model_event_handlers"


async def create_a_note() -> None:
    await Note.acreate(id=1, title="a")


async def update_a_note() -> None:
    await Note.aupdate(id=1, title="b")


async def test_a_handler_writing_a_model_with_listeners_leaves_the_native_lane() -> (
    None
):
    handlers = events.Handlers()

    @handlers.after_commit(Created[Note])
    async def created(event: Created[Note]) -> None: ...

    bus = bus_of(handlers)
    assert native_refusal(create_a_note) is None
    assert native_refusal(create_a_note, bus) == "Note has Created event handlers"
    assert native_refusal(update_a_note, bus) is None  # aupdate emits no event
