# 2. Schemas

A model describes a table; a `Schema` describes what crosses the API. A column
reaches a client only when a schema names it.

```python
# tracker/apps/tasks/schemas.py
from typing import Annotated

from hinoki import Field, Schema, derive

from .models import Task


class TaskIn(Schema):
    """What a client may set. The owner is the caller, never a field."""

    title: Annotated[str, Field(min_length=1, max_length=120)]
    priority: Annotated[int, Field(ge=1, le=5)] = 3


@derive(Task, exclude=("owner_id",))
class TaskOut(Schema):
    """Every column of a task except its owner (derived from the model)."""
```

- `TaskIn` is strict: no coercion, no unknown keys, and every problem is reported
  with its path. Validation runs in Rust before your code does. `priority` is
  optional and, when sent, must be from 1 to 5.
- `TaskOut` takes its fields from `Task`, with the model's types and limits.
  `exclude=("owner_id",)` keeps the owner out. Relations are never derived;
  you name them on purpose.
- The owner is not a field of `TaskIn`, so a client cannot choose it. Server
  values passed to `Task.acreate(data, owner_id=...)` always win, and a schema
  never carries the primary key.
- Every response is a `Schema`, never a model, a dict or a dataclass. The
  `members` app does the same with `MemberOut`, which names `id` and `name`
  and so never sends the token digest.

Schemas also support aliases, computed fields and tagged unions; see
[discriminated unions](https://hinoki.caspel.dev/dev/schema-collections.md#discriminated-unions).

## Recap

- Input schemas list what a client may send, nothing more.
- Output schemas can be derived from a model, minus what must stay private.
- Ownership comes from the caller, never from the request body.

Next: [3. Operations](https://hinoki.caspel.dev/dev/tutorial/3-operations.md).
