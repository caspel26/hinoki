"""Edge cases of the many-to-many engine: registration refusals, key types,
unusual scopes, reverse-side events and a prefetch ordered across chunks."""

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import ClassVar
from uuid import UUID, uuid4

import pytest

from hinoki import events
from hinoki.db import Database
from hinoki.errors import (
    FieldError,
    LinkLimitExceeded,
    ModelValidationError,
    RegistrationError,
    RelationNotLoaded,
    StoredLinksOverLimit,
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
from hinoki.orm import links as engine
from hinoki.orm.context import current_database
from hinoki.orm.migrations import plan
from hinoki.orm.options import ordered_by
from hinoki.settings import EventsSettings

URLS = {
    "postgres": os.environ.get("HINOKI_TEST_DATABASE_URL"),
    "mysql": os.environ.get("HINOKI_TEST_MYSQL_URL"),
    "mariadb": os.environ.get("HINOKI_TEST_MARIADB_URL"),
}


class Item(Model, table="m2mg_items"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str | None = None
    holders: ClassVar["ManyToMany[Holder]"]


@ordered_by("-score", "name")
class Ranked(Model, table="m2mg_ranked"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    score: int | None = None
    name: str = field(default="", max_length=20)


class Holder(Model, table="m2mg_holders"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    items: ClassVar[ManyToMany[Item]] = many_to_many(
        Item, related_name="holders", max_items=3
    )
    ranked: ClassVar[ManyToMany[Ranked]] = many_to_many(Ranked, max_items=2000)


class Code(Model, table="m2mg_codes"):
    id: str = field(primary_key=True, max_length=4)
    slots: ClassVar["ManyToMany[Slot]"]


class Slot(Model, table="m2mg_slots"):
    id: int = field(primary_key=True)
    codes: ClassVar[ManyToMany[Code]] = many_to_many(
        Code, related_name="slots", max_items=5
    )


MODELS: list[type[Model]] = [
    Item,
    Ranked,
    Code,
    Holder,
    Slot,
    *Holder.__bridges__,
    *Slot.__bridges__,
]
TABLES = [
    *(model.__table__ for model in reversed(MODELS)),
    "hinoki_migrations",
]


@pytest.fixture(params=["sqlite", "postgres", "mysql", "mariadb"])
async def database(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[Database]:
    kind = request.param
    url = f"sqlite://{tmp_path / 'g.sqlite3'}" if kind == "sqlite" else URLS[kind]
    if url is None:
        pytest.skip(f"the {kind} test database URL is not set")
    connection = await Database.connect(url, max_size=2, wait=0.5)
    await drop(connection)
    plan(MODELS, tmp_path, "initial", dialect=connection.dialect)
    await migrator.apply(connection, tmp_path)
    with use_database(connection):
        yield connection
    await drop(connection)
    connection.close()


async def drop(connection: Database) -> None:
    quote = "`" if connection.dialect in {"mysql", "mariadb"} else '"'
    for table in TABLES:
        await connection.execute(f"DROP TABLE IF EXISTS {quote}{table}{quote}")


@pytest.mark.usefixtures("database")
async def test_keys_of_every_supported_kind_link_and_unknown_shapes_are_refused() -> (
    None
):
    slot = await Slot.acreate(id=7)
    codes = [await Code.acreate(id=name) for name in ("a", "b", "cd")]
    await slot.codes.aadd(codes[0], "b")
    assert {c.id for c in await slot.codes.alist()} == {"a", "b"}
    await slot.codes.aset(["cd"])
    assert [c.id for c in await slot.codes.alist()] == ["cd"]
    for bad in (True, 3, 1.5, "toolong", None):
        with pytest.raises(ModelValidationError) as caught:
            await slot.codes.aadd(bad)  # type: ignore[arg-type]
        assert caught.value.details[0].code == "invalid_key"
    reverse = await Code.aget(id="cd")
    await reverse.slots.aadd(7)
    for bad in ("7", True, 1.5, 2**70):
        with pytest.raises(ModelValidationError):
            await reverse.slots.aadd(bad)  # type: ignore[arg-type]
    assert [s.id for s in await reverse.slots.alist()] == [7]


@pytest.mark.usefixtures("database")
async def test_the_reverse_side_enforces_the_declared_max_items() -> None:
    items = await Item.abulk_create([{"name": str(n)} for n in range(4)])
    holders = [await Holder.acreate() for _ in range(2)]
    shared = items[0]
    for holder in holders:
        await holder.items.aadd(shared)
    await shared.holders.aadd(*[h.id for h in holders])
    assert await shared.holders.acount() == 2
    await holders[0].items.aadd(items[1], items[2])
    with pytest.raises(LinkLimitExceeded):
        await holders[0].items.aadd(items[3])
    assert await holders[0].items.acount() == 3


@pytest.mark.usefixtures("database")
async def test_a_bridge_over_the_limit_is_a_server_fault_when_read() -> None:
    items = await Item.abulk_create([{"name": str(n)} for n in range(5)])
    holder = await Holder.acreate()
    quiet = await Holder.acreate()
    bridge = Holder.__bridges__[0]
    near, far = (c for c in bridge.__columns__ if c.endswith("_id"))
    await bridge.abulk_create(
        [{near: holder.id, far: item.id} for item in items[:2]]
        + [{near: quiet.id, far: items[0].id}],
        returning=False,
    )
    assert len(await Holder.all().prefetch_related("items")) == 2
    await bridge.abulk_create(
        [{near: holder.id, far: item.id} for item in items[2:]], returning=False
    )
    assert not isinstance(StoredLinksOverLimit("x", 1), ModelValidationError)
    with pytest.raises(StoredLinksOverLimit):
        await Holder.all().prefetch_related("items")


@pytest.mark.usefixtures("database")
async def test_scopes_that_are_not_conditions_are_refused() -> None:
    holder = await Holder.acreate()
    item = await Item.acreate(name="x")
    with pytest.raises(FieldError, match="must be a Item query"):
        await holder.items.aadd(item, within=Holder.all())  # type: ignore[arg-type]
    with pytest.raises(FieldError, match="owner scope must be a Holder query"):
        await holder.items.write("add", [item], None, Item.all())
    with pytest.raises(FieldError, match="may only filter"):
        await holder.items.write("add", [item], None, Holder.all().limit(1))
    assert await holder.items.acount() == 0


@pytest.mark.usefixtures("database")
async def test_link_filters_name_a_field_and_do_not_mix_isnull() -> None:
    with pytest.raises(FieldError, match="with a field"):
        Holder.filter(items="x")
    with pytest.raises(FieldError, match="cannot be combined"):
        Holder.filter(items__isnull=True, items__name="x")
    assert Holder.all().filter(items__name="x").subfilters == (
        Holder.filter(items__name="x").subfilters
    )


@pytest.mark.usefixtures("database")
async def test_loaded_ids_and_unloaded_reads_are_explicit() -> None:
    item = await Item.acreate(name="x")
    holder = await Holder.acreate()
    await holder.items.aadd(item)
    with pytest.raises(RelationNotLoaded):
        _ = holder.items.loaded_ids
    with pytest.raises(RelationNotLoaded):
        _ = holder.items_ids  # type: ignore[attr-defined]
    assert Holder.items_ids is not None  # type: ignore[attr-defined]
    (loaded,) = await Holder.all().prefetch_related("items")
    assert loaded.items.loaded_ids == (item.id,)
    assert loaded.items_ids == (item.id,)  # type: ignore[attr-defined]
    with pytest.raises(AttributeError, match="read-only"):
        loaded.items_ids = ()  # type: ignore[attr-defined]
    with pytest.raises(AttributeError, match="add/remove/set"):
        loaded.items = ()  # type: ignore[misc]
    assert (await holder.items.aadd()).added == ()


@pytest.mark.usefixtures("database")
async def test_a_link_write_needs_its_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder = await Holder.acreate()
    item = await Item.acreate(name="x")
    database = current_database()
    monkeypatch.setattr(database, "active", lambda: None)
    with pytest.raises(Exception, match="inside a transaction"):
        await holder.items.aadd(item)


@pytest.mark.usefixtures("database")
async def test_the_reverse_side_emits_one_event_per_declaring_row() -> None:
    seen: list[tuple[object, tuple[object, ...]]] = []
    handlers = events.Handlers()

    @handlers.after_commit(events.Linked[Holder])
    async def linked(event: events.Linked[Holder]) -> None:
        seen.append((event.key, event.targets))

    bus = events.EventBus([handlers], EventsSettings())
    item = await Item.acreate(name="x")
    first, second = await Holder.acreate(), await Holder.acreate()
    with events.use(bus):
        await item.holders.aadd(first, second)
    assert sorted(seen, key=repr) == sorted(
        [(first.id, (item.id,)), (second.id, (item.id,))], key=repr
    )


@pytest.mark.usefixtures("database")
async def test_prefetched_targets_keep_one_global_order_across_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine, "IN_CHUNK", 3)
    names = ["m", "c", "x", "a", "q", "b", "z", "k", "d", "f"]
    scores = [None, 2, 1, 2, None, 0, 1, 2, 0, None]
    rows = await Ranked.abulk_create(
        [
            {"score": score, "name": name}
            for score, name in zip(scores, names, strict=True)
        ]
    )
    holder = await Holder.acreate()
    await holder.ranked.aset(rows)
    (loaded,) = await Holder.all().prefetch_related("ranked")
    shown = [(r.score, r.name) for r in loaded.ranked.loaded]
    # Highest score first, a missing score last, ties by name: whatever the
    # chunks and the database's own NULL placement.
    assert shown == sorted(
        zip(scores, names, strict=True),
        key=lambda pair: (-(pair[0] if pair[0] is not None else -1), pair[1]),
    )


@pytest.mark.usefixtures("database")
async def test_more_than_a_thousand_targets_are_prefetched_in_one_order() -> None:
    rows = await Ranked.abulk_create(
        [{"score": n % 7, "name": f"n{n:04}"} for n in range(1200)], returning=False
    )
    assert rows == [] or len(rows) == 1200
    holder = await Holder.acreate()
    everyone = await Ranked.all()
    await holder.ranked.aset(everyone)
    (loaded,) = await Holder.all().prefetch_related("ranked")
    keys = [(-(r.score or 0), r.name) for r in loaded.ranked.loaded]
    assert len(keys) == 1200
    assert keys == sorted(keys)


def declare(table: str, **namespace: object) -> type[Model]:
    return type(
        "Declared" + table,
        (Model,),
        {"__annotations__": {"id": UUID}, "id": field(primary_key=True), **namespace},
        table=table,
    )


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"target": "elsewhere"}, "Model class or 'self'"),
        ({"columns": ("A_id", "b_id")}, "lower-case identifier"),
        ({"columns": ("a_id", "a_id")}, "must differ"),
        ({"ids_name": "same", "related_name": "same"}, "names must differ"),
        ({"ids_name": "id"}, "already has an attribute"),
    ],
)
def test_registration_refuses_bad_declarations(
    options: dict[str, object], message: str
) -> None:
    arguments = {"target": Item, **options}
    target = arguments.pop("target")
    with pytest.raises(RegistrationError, match=message):
        declare(
            "m2mg_bad",
            tags=many_to_many(target, **arguments),  # type: ignore[call-overload]
        )


def test_an_abstract_model_cannot_declare_a_relation() -> None:
    with pytest.raises(RegistrationError, match="abstract"):
        type(
            "AbstractHolder",
            (Model,),
            {
                "__annotations__": {"tags": ClassVar[ManyToMany[Item]]},
                "tags": many_to_many(Item),
            },
            abstract=True,
        )


def test_keys_of_a_kind_the_bridge_cannot_hold_are_refused() -> None:
    class Weighed(Model, table="m2mg_weighed"):
        id: float = field(primary_key=True)

    class Bare(Model, table="m2mg_bare"):
        id: str = field(primary_key=True)

    for target, message in ((Weighed, "UUID, int or text"), (Bare, "max_length")):
        with pytest.raises(RegistrationError, match=message):
            declare("m2mg_k" + target.__name__.lower(), tags=many_to_many(target))


def test_an_adopted_bridge_must_be_a_plain_uuid_keyed_pair() -> None:
    class Source(Model, table="m2mg_source"):
        id: UUID = field(primary_key=True, default_factory=uuid4)
        items: ClassVar[ManyToMany[Item]] = many_to_many(Item, bridge="IntBridge")

    with pytest.raises(RegistrationError, match="UUID"):

        class IntBridge(
            Model, table="m2mg_int_bridge", unique=[("source_id", "item_id")]
        ):
            id: int = field(primary_key=True)
            source_id: UUID = foreign_key(Source, on_delete=OnDelete.CASCADE)
            item_id: UUID = foreign_key(Item, on_delete=OnDelete.CASCADE)

    class Loose(Model, table="m2mg_loose_owner"):
        id: UUID = field(primary_key=True, default_factory=uuid4)
        items: ClassVar[ManyToMany[Item]] = many_to_many(
            Item, bridge="WrongBridge", columns=("loose_owner_id", "item_id")
        )

    with pytest.raises(RegistrationError, match="foreign key to Item"):

        class WrongBridge(
            Model, table="m2mg_wrong_bridge", unique=[("loose_owner_id", "item_id")]
        ):
            id: UUID = field(primary_key=True, default_factory=uuid4)
            loose_owner_id: UUID = foreign_key(Loose, on_delete=OnDelete.CASCADE)
            item_id: UUID = foreign_key(Source, on_delete=OnDelete.CASCADE)


@pytest.mark.usefixtures("database")
async def test_unknown_targets_still_roll_the_write_back() -> None:
    holder = await Holder.acreate()
    item = await Item.acreate(name="x")
    with pytest.raises(UnknownLinkTarget):
        await holder.items.aadd(item, uuid4())
    assert await holder.items.acount() == 0


def test_a_relation_waiting_for_its_bridge_says_so() -> None:
    class Waiting(Model, table="m2mg_waiting"):
        id: UUID = field(primary_key=True, default_factory=uuid4)
        items: ClassVar[ManyToMany[Item]] = many_to_many(Item, bridge="NeverDefined")

    with pytest.raises(RegistrationError, match="waiting for its bridge"):
        _ = Waiting().items
