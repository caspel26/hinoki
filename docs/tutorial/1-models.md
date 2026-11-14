# 1. Models

A model is a table. Hinoki's models are active records over a Rust query compiler: no lazy loading, no surprises.

```python
from datetime import datetime
from uuid import UUID, uuid4

from hinoki.orm import Model, field, foreign_key, indexed, ordered_by
from hinoki.utils import utc_now


class Member(Model, table="tracker_members"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str = field(max_length=80)
    token: str = field(unique=True)


@indexed("owner_id", "created_at")
@ordered_by("-created_at", "-id")
class Task(Model, table="tracker_tasks", label="Task", plural="Tasks"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    owner_id: UUID = foreign_key(Member, on_delete="restrict", related_name="tasks")
    title: str = field(max_length=120)
    done: bool = False
    created_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None
```

* `table=` is a class keyword because it is needed when the class is created.
* `@indexed` and `@ordered_by` are decorators that state facts about the model below them ([model options](../MODEL-OPTIONS.md)). The default order applies to every query that does not call `order_by`.
* `foreign_key(Member, ...)` is a column holding the owner's id; the relation (`task.owner`) is never fetched by accident.

## The migration

The planner compares the models with its recorded state and writes SQL you can read:

```sh
hinoki migrations --dir examples/tracker/migrations plan \
    --models examples.tracker.models --name initial
```

```sql
CREATE TABLE "tracker_tasks" ( ... );
CREATE INDEX "ix_tracker_tasks_owner_id_created_at" ON "tracker_tasks" ("owner_id", "created_at");
```

Review it like code, apply it with `migrations apply` against a disposable database first, never at startup. Destructive changes are flagged and need `--allow-destructive`.

Next: [2. Schemas](2-schemas.md).
