"""The public time and model timestamp helpers remain statically usable."""

from datetime import datetime
from uuid import UUID, uuid4

from hinoki.orm import Model, field, timestamps
from hinoki.utils import Clock, utc_now


def fixed_clock() -> datetime:
    return utc_now()


clock: Clock = fixed_clock


@timestamps(clock=clock)
class Event(Model, table="events"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


def construct() -> Event:
    return Event(name="created")
