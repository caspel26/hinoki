"""date, time, timedelta, Decimal and Enum as Model columns."""

import datetime as dt
import enum
import os
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hinoki.db import Database
from hinoki.errors import FieldError, ModelValidationError
from hinoki.orm import Model, field, use_database
from hinoki.orm.migrations import create_table_sql, table_state


class Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


class Level(enum.IntEnum):
    LOW = 1
    HIGH = 10


class Entry(Model, table="scalar_entries"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    day: dt.date
    at: dt.time | None = None
    span: dt.timedelta | None = None
    price: Decimal | None = None
    color: Color | None = None
    level: Level | None = None


def test_the_columns_have_native_or_text_types() -> None:
    columns = table_state(Entry)["columns"]
    assert {name: columns[name]["type"] for name in columns} == {
        "id": "uuid",
        "day": "text",
        "at": "text",
        "span": "bigint",
        "price": "text",
        "color": "text",
        "level": "bigint",
    }
    assert columns["at"]["nullable"] is True
    assert columns["day"]["nullable"] is False


def test_a_wrong_value_is_refused_before_a_write() -> None:
    day = dt.date(2026, 1, 2)
    for bad in (
        {"day": "2026-01-02"},
        {"day": dt.datetime(2026, 1, 2, tzinfo=dt.UTC)},
        {"day": day, "at": "10:00"},
        {"day": day, "at": dt.time(10, tzinfo=dt.UTC)},
        {"day": day, "span": 5},
        {"day": day, "span": dt.timedelta.max},
        {"day": day, "price": 1.5},
        {"day": day, "price": Decimal("NaN")},
        {"day": day, "color": "red"},
        {"day": day, "color": Level.LOW},
        {"day": day, "level": 1},
    ):
        with pytest.raises(ModelValidationError):
            Entry.validate_values(bad)


BACKENDS = {
    "sqlite": None,
    "postgres": os.environ.get("HINOKI_TEST_DATABASE_URL"),
    "mysql": os.environ.get("HINOKI_TEST_MYSQL_URL"),
    "mariadb": os.environ.get("HINOKI_TEST_MARIADB_URL"),
}


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
async def db(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Database]:
    backend: str = request.param
    url = BACKENDS[backend] or f"sqlite://{tmp_path / 'scalars.sqlite3'}"
    database = await Database.connect(url, max_size=2, wait=0.5)
    await database.execute("DROP TABLE IF EXISTS scalar_entries")
    await database.execute(
        create_table_sql(Entry.__table__, table_state(Entry), backend)
    )
    with use_database(database):
        yield database
    await database.execute("DROP TABLE IF EXISTS scalar_entries")
    database.close()


async def test_round_trip_and_filter(db: Database) -> None:
    created = await Entry.acreate(
        day=dt.date(2026, 3, 4),
        at=dt.time(9, 30, 5, 250),
        span=dt.timedelta(days=-2, seconds=7, microseconds=3),
        price=Decimal("12.3400"),
        color=Color.BLUE,
        level=Level.HIGH,
    )
    loaded = await Entry.aget(id=created.id)
    assert loaded.day == dt.date(2026, 3, 4)
    assert loaded.at == dt.time(9, 30, 5, 250)
    assert loaded.span == dt.timedelta(days=-2, seconds=7, microseconds=3)
    assert loaded.price == Decimal("12.3400")
    assert str(loaded.price) == "12.3400"
    assert loaded.color is Color.BLUE
    assert loaded.level is Level.HIGH
    assert await Entry.filter(day=dt.date(2026, 3, 4)).acount() == 1
    assert await Entry.filter(color=Color.BLUE).acount() == 1
    assert await Entry.filter(level=Level.LOW).acount() == 0
    updated = await Entry.aupdate(id=created.id, color=Color.RED, at=None)
    assert updated.color is Color.RED
    assert updated.at is None


async def test_dates_and_durations_compare_and_order(db: Database) -> None:
    for day, span in ((3, 30), (1, -5), (2, 4)):
        await Entry.acreate(day=dt.date(2026, 1, day), span=dt.timedelta(seconds=span))
    later = await Entry.filter(day__gt=dt.date(2026, 1, 1)).acount()
    longer = await Entry.filter(span__gte=dt.timedelta(seconds=4)).acount()
    ordered = await Entry.all().order_by("-span")
    assert (later, longer) == (2, 2)
    assert [e.span.total_seconds() for e in ordered if e.span] == [30, 4, -5]
    days = await Entry.all().order_by("day")
    assert [e.day.day for e in days] == [1, 2, 3]


async def test_text_order_is_refused_where_it_would_be_wrong(db: Database) -> None:
    with pytest.raises(FieldError, match="price"):
        Entry.filter(price__gt=Decimal(1))
    with pytest.raises(FieldError, match="price"):
        Entry.filter(price=Decimal(1))
    with pytest.raises(FieldError, match="price"):
        Entry.all().order_by("price")
    assert await Entry.filter(price__isnull=True).acount() == 0


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("day", "'yesterday'"),
        ("at", "'noon'"),
        ("price", "'abc'"),
        ("color", "'green'"),
        ("level", "7"),
    ],
)
async def test_a_corrupt_stored_value_is_a_field_error(
    db: Database, column: str, value: str
) -> None:
    await Entry.acreate(day=dt.date(2026, 1, 1))
    await db.execute(f"UPDATE scalar_entries SET {column} = {value}")  # noqa: S608
    with pytest.raises(FieldError, match=column):
        await Entry.all().afirst()
