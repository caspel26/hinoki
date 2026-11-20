---
status: implemented
related:
  - models.md
  - model-options.md
  - testing.md
---

# Time and utilities

Hinoki ships a few helpers where the framework defines the behaviour. Use the
standard library for everything else.

## UTC time

`utc_now()` returns a timezone-aware `datetime` in UTC; use it for field
factories and application code.

```python
# notes/apps/billing/models.py
from datetime import datetime
from uuid import UUID, uuid4

from hinoki.orm import Model, field, pk
from hinoki.utils import utc_now


class Event(Model):
    id: UUID = pk(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)
```

A clock is any callable returning an aware `datetime` (`hinoki.utils.Clock`).
Code that depends on time accepts a clock; tests pass a fixed one. No
process-wide patching is needed.

`utc_today(clock=utc_now)` returns the date in UTC, independent of the
machine's timezone: `2026-01-02 00:30+02:00` is `2026-01-01`.

## Test clocks

```python
# tests/clocktest.py
from datetime import UTC, datetime

from hinoki.testing import ManualClock


def test_clock_moves() -> None:
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=UTC))
    first = clock()
    clock.advance(seconds=60)
    assert (clock() - first).total_seconds() == 60
```

`ManualClock` stores UTC instants, accepts only aware values and works with
`app.now(clock)`, `utc_today(clock)` and `@db.timestamps(clock=...)`.

## Application timezone

```python
# notes/settings.py
from hinoki import settings


class ProjectSettings(settings.AppSettings, prefix="NOTES_", dotenv=".env"):
    timezone: str = "Europe/Rome"
```

`app.now()` returns an aware `datetime` in the settings' IANA timezone
(default `UTC`); an invalid name fails while settings load. Apps in one
process can use different timezones; there is no `Hinoki(timezone=...)`.
`app.now(clock)` takes a clock for tests.

## Model update timestamps

```python
# notes/apps/billing/models.py (continued)
@db.timestamps()
class Statement(Model):
    id: UUID = pk(default_factory=uuid4)
    title: str = field(max_length=120)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
```

* `@db.timestamps()` sets the non-nullable `updated_at: datetime` column on every
  ORM update: `Model.aupdate`, `aupdate_where`, `asave`, `aupdate_from`,
  `abulk_update`, `QuerySet.aupdate` and their sync forms. One statement gives
  every row the same time; a value supplied for the column is replaced.
* `@db.timestamps("modified_at", clock=my_clock)` names another column or injects
  a clock; clock values must be aware and are stored in UTC.
* Creation uses the field factories. Raw SQL bypasses the decorator: use a
  database trigger if independent writers need database-authoritative times.
* Both columns are ordinary fields, visible to type checkers and migrations.

## Other common needs

| Need | Use |
| --- | --- |
| UUID identifiers | `uuid.uuid4()` |
| secret tokens | `secrets`, or `hinoki.crypto.random_token()` for Hinoki authentication |
| file system paths | `pathlib.Path` |
| configuration | [settings](settings.md) |
| requests in tests | `hinoki.testing` clients ([testing](testing.md)) |

Hinoki has no slug, retry or environment helpers: their rules belong to the
application.

## See also

- [Model options](model-options.md)
- [Testing](testing.md)
