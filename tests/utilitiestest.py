"""General time helpers and ORM timestamp behavior."""

import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from uuid import UUID, uuid4

import pytest

from hinoki import FrameworkSettings, Hinoki, RegistrationError
from hinoki.db import Database
from hinoki.orm import Model, field, timestamps, use_database
from hinoki.utils import _read_utc, utc_now, utc_today

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
needs_database = pytest.mark.skipif(URL is None, reason="no database")
clock_values = [datetime(2026, 1, 2, 3, 4, tzinfo=timezone(timedelta(hours=2)))]


def test_clock_values_are_aware_and_normalized_to_utc() -> None:
    assert utc_now().tzinfo is UTC
    assert _read_utc(lambda: clock_values[0]) == datetime(2026, 1, 2, 1, 4, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone-aware"):
        _read_utc(lambda: datetime(2026, 1, 2))


def test_utc_today_uses_utc_calendar_boundary() -> None:
    value = datetime(2026, 1, 2, 0, 30, tzinfo=timezone(timedelta(hours=2)))
    assert utc_today(lambda: value) == date(2026, 1, 1)
    assert isinstance(utc_today(), date)


def test_app_now_uses_each_app_settings_without_shared_state() -> None:
    class RomeSettings(FrameworkSettings):
        timezone: str = "Europe/Rome"

    class NewYorkSettings(FrameworkSettings):
        timezone: str = "America/New_York"

    instant = datetime(2026, 1, 1, 23, 30, tzinfo=UTC)
    rome = Hinoki(settings=RomeSettings)
    new_york = Hinoki(settings=NewYorkSettings())
    assert rome.now(lambda: instant).isoformat() == "2026-01-02T00:30:00+01:00"
    assert new_york.now(lambda: instant).isoformat() == "2026-01-01T18:30:00-05:00"


def test_app_timezone_defaults_to_utc_and_settings_reject_invalid_zone() -> None:
    assert Hinoki().now(lambda: clock_values[0]).utcoffset() == timedelta(0)
    with pytest.raises(ValueError, match="IANA timezone"):
        FrameworkSettings(timezone="Not/AZone")
    with pytest.raises(ValueError, match="IANA timezone"):
        FrameworkSettings(timezone=None)  # type: ignore[arg-type]


def test_app_now_rejects_a_naive_clock() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Hinoki().now(lambda: datetime(2026, 1, 2))


def test_app_now_rejects_a_clock_with_an_invalid_timezone() -> None:
    class InvalidTimezone(tzinfo):
        def utcoffset(self, dt: datetime | None) -> timedelta | None:
            raise ValueError("invalid offset")

        def dst(self, dt: datetime | None) -> timedelta | None:
            return None

        def tzname(self, dt: datetime | None) -> str | None:
            return None

    with pytest.raises(ValueError, match="invalid timezone"):
        Hinoki().now(lambda: datetime(2026, 1, 2, tzinfo=InvalidTimezone()))

    class NoOffsetTimezone(tzinfo):
        def utcoffset(self, dt: datetime | None) -> timedelta | None:
            return None

        def dst(self, dt: datetime | None) -> timedelta | None:
            return None

        def tzname(self, dt: datetime | None) -> str | None:
            return None

    with pytest.raises(ValueError, match="timezone-aware"):
        Hinoki().now(lambda: datetime(2026, 1, 2, tzinfo=NoOffsetTimezone()))


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
