# 2. Schemas

A model describes a table; a `Schema` describes what crosses the API. A column reaches a client only when a schema names it.

```python
from typing import Annotated

from examples.tracker.models import Task
from hinoki import Field, Schema


class TaskIn(Schema):
    """What a client may set. The owner is the caller, never a field."""

    title: Annotated[str, Field(min_length=1, max_length=120)]


class TaskOut(Schema, model=Task, exclude=["owner_id"]):
    """Every column of a task except its owner (derived from the model)."""
```

* `TaskIn` is strict: no coercion, no unknown keys, every problem reported with its path. Validation runs in Rust before your code does.
* `TaskOut` takes the columns of `Task` with `model=`: types and limits come from the model, `exclude` keeps `owner_id` out. Relations are never derived; you name them on purpose.
* Because the owner is not a field of `TaskIn`, a client cannot choose it. If it were, `Task.acreate(data, owner_id=me)` would still win (server values beat the client's), and a schema can never carry the primary key.

More: [Schemas](../SCHEMAS.md), [schemas from models and validators](../MODEL-SCHEMA-DESIGN.md).

Next: [3. Operations](3-operations.md).
