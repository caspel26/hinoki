"""The public time and model timestamp helpers remain statically usable."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

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
