"""General time helpers and ORM timestamp behavior."""

import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from hinoki import RegistrationError
from hinoki.db import Database
from hinoki.orm import Model, field, timestamps, use_database
from hinoki.utils import _read_utc, utc_now

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
needs_database = pytest.mark.skipif(URL is None, reason="no database")
clock_values = [datetime(2026, 1, 2, 3, 4, tzinfo=timezone(timedelta(hours=2)))]


def test_clock_values_are_aware_and_normalized_to_utc() -> None:
    assert utc_now().tzinfo is UTC
    assert _read_utc(lambda: clock_values[0]) == datetime(2026, 1, 2, 1, 4, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone-aware"):
        _read_utc(lambda: datetime(2026, 1, 2))


@pytest.fixture(autouse=True)
def reset_clock() -> Iterator[None]:
    initial = clock_values[0]
    yield
    clock_values[0] = initial


@timestamps(clock=lambda: clock_values[0])
class Stamp(Model, table="ut_stamps"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    value: str
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


def test_timestamp_option_rejects_unknown_or_non_datetime_columns() -> None:
    with pytest.raises(RegistrationError, match="unknown"):

        @timestamps("missing")
        class Missing(Model, table="ut_missing"):
            id: UUID = field(primary_key=True, default_factory=uuid4)
            created_at: datetime = field(default_factory=utc_now)

    with pytest.raises(RegistrationError, match="must be datetime"):

        @timestamps("value")
        class WrongType(Model, table="ut_wrong_type"):
            id: UUID = field(primary_key=True, default_factory=uuid4)
            value: str

    with pytest.raises(RegistrationError, match="cannot be the primary key"):

        @timestamps("id")
        class InvalidPrimaryTimestamp(Model, table="invalid_primary_timestamps"):
            id: datetime = field(primary_key=True, default_factory=utc_now)
            value: str


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    assert URL is not None
    db = await Database.connect(URL, max_size=1)
    await db.execute("DROP TABLE IF EXISTS ut_stamps")
    await db.execute(
        "CREATE TABLE ut_stamps (id uuid PRIMARY KEY, value text NOT NULL, "
        "created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL)"
    )
    with use_database(db):
        yield db
    await db.execute("DROP TABLE IF EXISTS ut_stamps")
    db.close()


@needs_database
async def test_timestamps_are_applied_to_model_and_queryset_update_paths(
    database: Database,
) -> None:
    row = await Stamp.acreate(value="initial")
    assert row.created_at.utcoffset() == timedelta(0)

    expected_first = datetime(2026, 1, 2, 1, 4, tzinfo=UTC)
    row.value = "instance"
    row.updated_at = datetime(2000, 1, 1, tzinfo=UTC)
    await row.asave()
    assert row.updated_at == expected_first

    clock_values[0] = datetime(2026, 1, 3, 6, 30, tzinfo=timezone(timedelta(hours=-4)))
    expected_second = datetime(2026, 1, 3, 10, 30, tzinfo=UTC)
    values = {"value": "queryset", "updated_at": datetime(1999, 1, 1, tzinfo=UTC)}
    rows = await Stamp.filter(id=row.id).aupdate(values)
    assert rows[0].updated_at == expected_second
    assert values["updated_at"] == datetime(1999, 1, 1, tzinfo=UTC)

    clock_values[0] = datetime(2026, 1, 4, tzinfo=UTC)
    updated_row = await Stamp.aupdate(id=row.id, value="model")
    assert updated_row.updated_at == clock_values[0]

    clock_values[0] = datetime(2026, 1, 5, tzinfo=UTC)
    updated_rows = await Stamp.aupdate_where({"id": row.id}, {"value": "where"})
    assert updated_rows[0].updated_at == clock_values[0]

    clock_values[0] = datetime(2026, 1, 6, tzinfo=UTC)
    row = updated_rows[0]
    await row.aupdate_from(value="instance-method")
    assert row.updated_at == clock_values[0]
