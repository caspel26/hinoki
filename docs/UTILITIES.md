---
status: implemented
related:
  - MODELS.md
  - TESTING.md
---

# Utilities

Hinoki includes a small set of helpers where the framework can define clear behavior. Use the Python standard library for ordinary IDs, paths, and application-specific formatting.

## UTC time

`utc_now()` returns a timezone-aware `datetime` in UTC. It is suitable for field factories and application code:

```python
from datetime import datetime
from uuid import UUID, uuid4

from hinoki.orm import Model, field
from hinoki.utils import utc_now


class Event(Model, table="events"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)
```

The clock is a callable, so code that depends on time can accept a `Clock` and tests can supply a fixed function. No process-wide clock patch is needed.

## Model update timestamps

Declare the columns explicitly and decorate the model with `@timestamps()` to refresh `updated_at` on each ORM update:

```python
from datetime import datetime
from uuid import UUID, uuid4

from hinoki.orm import Model, field, timestamps
from hinoki.utils import utc_now


@timestamps()
class Note(Model, table="notes"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
```

The decorator requires a non-nullable `datetime` field named `updated_at`. Use `@timestamps("modified_at", clock=my_clock)` for another field or an injected clock. Clock values must be timezone-aware; Hinoki converts them to UTC. A value supplied for the managed field in an update is replaced by the clock value.

The behavior applies to `Model.aupdate`/`update_where`, instance `asave`/`aupdate_from`, and `QuerySet.aupdate`, including sync counterparts. All rows changed by one statement receive the same time. Creation uses the declared field factories. Raw SQL bypasses this ORM behavior; use an explicit database trigger if independent writers need database-authoritative timestamps.

These fields are ordinary model columns. They remain visible to type checkers and the migration planner; Hinoki does not add hidden fields or apply migrations automatically.

## Other common needs

* Use `uuid.uuid4()` for ordinary UUID identifiers.
* Use `secrets` or `hinoki.crypto.random_token` for secret tokens, depending on whether the token is part of Hinoki authentication.
* Use `pathlib.Path` for filesystem paths and the existing `Settings` loader for environment and dotenv configuration.
* Use `hinoki.testing` clients for application requests; inject a clock callable when behavior depends on time.

Hinoki does not provide generic slug, retry, or environment helpers because their rules depend on the application.
