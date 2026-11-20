"""The public time and model timestamp helpers remain statically usable."""

from datetime import UTC, datetime
from typing import assert_type
from uuid import UUID, uuid4

from hinoki import db
from hinoki.orm import Model, field, timestamps
from hinoki.testing import ManualClock
from hinoki.utils import Clock, utc_now


def fixed_clock() -> datetime:
    return utc_now()


clock: Clock = fixed_clock
manual_clock: Clock = ManualClock(datetime(2026, 1, 1, tzinfo=UTC))


@timestamps(clock=clock)
class Event(Model, table="events"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


def construct() -> Event:
    return Event(name="created")


@db.unique("owner", "name", name="uq_ledger_owner_name")
@db.timestamps(clock=clock)
class Ledger(Model, table="ledgers"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    owner: str
    name: str
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


def construct_ledger() -> Ledger:
    ledger = Ledger(owner="o", name="n")
    assert_type(ledger.updated_at, datetime)
    return ledger
