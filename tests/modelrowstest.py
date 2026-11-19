"""``QuerySet.fetch`` builds instances natively; they must equal ``from_row``'s."""

import dataclasses
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from hinoki import core
from hinoki.db import Database
from hinoki.errors import FieldError
from hinoki.orm import Model, OnDelete, field, foreign_key, use_database
from hinoki.orm.migrations import create_table_sql, table_state
from hinoki.orm.model import SNAPSHOT


class Shelf(Model, table="rows_shelves"):
    id: int = field(primary_key=True)
    name: str = ""


class Item(Model, table="rows_items"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    label: str = ""
    weight: float = 0.0
    count: int = 0
    active: bool = True
    note: str | None = None
    raw: bytes = b""
    taken_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    tags: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    shelf_id: int | None = foreign_key(
        Shelf, on_delete=OnDelete.CASCADE, related_name="items"
    )


class Loose(Model, table="rows_loose"):
    id: int = field(primary_key=True)
    meta: dict[str, Any] = field(default_factory=dict)


class Custom(Model, table="rows_custom"):
    id: int = field(primary_key=True)
    name: str = ""

    def __post_init__(self) -> None:
        self.name = self.name.upper()


BACKENDS = {
    "sqlite": None,
    "postgres": os.environ.get("HINOKI_TEST_DATABASE_URL"),
    "mysql": os.environ.get("HINOKI_TEST_MYSQL_URL"),
    "mariadb": os.environ.get("HINOKI_TEST_MARIADB_URL"),
}
TABLES = ("rows_items", "rows_shelves", "rows_custom", "rows_loose")


@pytest.fixture(
    params=[
        pytest.param(
            name,
            marks=pytest.mark.skipif(
                name != "sqlite" and url is None, reason=f"no {name} database URL"
            ),
        )
        for name, url in BACKENDS.items()
    ]
)
async def database(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[Database]:
    backend: str = request.param
    url = BACKENDS[backend] or f"sqlite://{tmp_path / 'rows.sqlite3'}"
    database = await Database.connect(url)
    for table in TABLES:
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    for model in (Shelf, Item, Custom):
        await database.execute(
            create_table_sql(model.__table__, table_state(model), backend)
        )
    with use_database(database):
        yield database
    for table in TABLES:
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    database.close()


def hole(database: Database) -> str:
    return "$1" if database.dialect == "postgres" else "?"


async def seed() -> None:
    await Shelf(id=1, name="top").insert()
    for index in range(3):
        await Item(
            label=f"item {index}",
            weight=index + 0.5,
            count=index,
            active=bool(index % 2),
            note=None if index else "first",
            raw=bytes([index]),
            tags=["a", str(index)],
            meta={"nested": {"n": index}, "list": [1, 2]},
            shelf_id=1,
        ).insert()


def oracle(item_class: type[Model], row: dict[str, Any]) -> Model:
    return item_class.from_row(row)


async def test_native_rows_equal_from_row(database: Database) -> None:
    await seed()
    assert Item.row_plan() is not None
    sql, params = Item.all().order_by("count").compile(dialect=database.dialect)
    rows = await database.fetch(sql, *params)

    native = await Item.all().order_by("count")
    expected = [Item.from_row(row) for row in rows]

    assert len(native) == 3
    for found, wanted in zip(native, expected, strict=True):
        assert type(found) is type(wanted)
        assert found == wanted
        assert list(found.__dict__) == list(wanted.__dict__)
        assert found.__dict__ == wanted.__dict__
        assert found.__dict__[SNAPSHOT] == wanted.__dict__[SNAPSHOT]
        assert not found.is_dirty


async def test_snapshot_copies_mutable_values_and_shares_scalars(
    database: Database,
) -> None:
    await seed()
    found = (await Item.all().order_by("count"))[0]
    snapshot = found.__dict__[SNAPSHOT]
    assert snapshot["tags"] == found.tags
    assert snapshot["tags"] is not found.tags
    assert snapshot["meta"] is not found.meta
    assert snapshot["label"] is found.label
    assert snapshot["id"] is found.id

    found.tags.append("changed")
    found.meta["nested"]["n"] = 99
    assert found.is_dirty
    assert set(found.changes) == {"tags", "meta"}
    await found.asave()
    again = await Item.aget(id=found.id)
    assert again.tags == ["a", "0", "changed"]
    assert again.meta["nested"] == {"n": 99}


async def test_zero_rows_and_transaction_scope(database: Database) -> None:
    assert await Item.all() == []
    await seed()
    async with database.atomic():
        inside = await Item.filter(count=1)
    assert [item.count for item in inside] == [1]


async def test_invalid_json_text_is_a_field_error(database: Database) -> None:
    await database.execute(
        "CREATE TABLE rows_loose (id INTEGER PRIMARY KEY, meta TEXT)"
    )
    await database.execute(
        f"INSERT INTO rows_loose VALUES (1, {hole(database)})",  # noqa: S608 - constant
        "{not json",
    )
    assert Loose.row_plan() is not None
    with pytest.raises(FieldError, match=r"Loose\.meta: database value is not valid"):
        await Loose.all()
    with pytest.raises(FieldError, match=r"Loose\.meta: database value is not valid"):
        Loose.from_row({"id": 1, "meta": "{not json"})

    await database.execute(
        f"UPDATE rows_loose SET meta = {hole(database)}",  # noqa: S608 - constant
        '{"a": [1, 2]}',
    )
    assert (await Loose.all())[0].meta == {"a": [1, 2]}


async def test_joined_and_custom_models_keep_the_python_path(
    database: Database,
) -> None:
    await seed()
    await database.execute("INSERT INTO rows_custom (id, name) VALUES (1, 'abc')")
    assert Custom.row_plan() is None
    assert [c.name for c in await Custom.all()] == ["ABC"]
    joined = await Item.all().select_related("shelf").order_by("count")
    assert joined[0].related("shelf").name == "top"
    assert all(item.__dict__[SNAPSHOT] for item in joined)


def test_row_plan_is_cached_per_class() -> None:
    first = Item.row_plan()
    assert isinstance(first, core._ModelRows)
    assert Item.row_plan() is first
    assert dataclasses.is_dataclass(Item)


def test_overridden_from_row_keeps_the_python_path() -> None:
    class Tracked(Model, table="rows_tracked"):
        id: int = field(primary_key=True)

        @classmethod
        def from_row(cls, row: dict[str, Any]) -> "Tracked":
            return super().from_row(row)

    assert Tracked.row_plan() is None


async def test_hooks_patched_after_the_first_fetch_are_honoured(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed()
    assert len(await Item.all()) == 3
    seen: list[dict[str, Any]] = []
    original = Item.from_row.__func__  # type: ignore[attr-defined]

    def spy(cls: type[Item], row: dict[str, Any]) -> Item:
        seen.append(row)
        return original(cls, row)  # type: ignore[no-any-return]

    monkeypatch.setattr(Item, "from_row", classmethod(spy))
    assert len(await Item.all()) == 3
    assert len(seen) == 3


async def test_other_value_errors_are_not_reported_as_invalid_json(
    database: Database,
) -> None:
    await database.execute(
        "CREATE TABLE rows_loose (id INTEGER PRIMARY KEY, meta TEXT)"
    )
    await database.execute(
        f"INSERT INTO rows_loose VALUES (1, {hole(database)})",  # noqa: S608 - constant
        "1" * 5000,
    )
    with pytest.raises(ValueError, match="limit") as native:
        await Loose.all()
    with pytest.raises(ValueError, match="limit") as python:
        Loose.from_row({"id": 1, "meta": "1" * 5000})
    assert not isinstance(native.value, FieldError)
    assert type(native.value) is type(python.value)
