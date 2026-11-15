# 1. Models

A model is a table. Hinoki's models are active records over a Rust query compiler: no lazy loading, no surprises.

```python
from datetime import datetime
from uuid import UUID, uuid4

from hinoki.orm import Model, OnDelete, field, foreign_key, indexed, ordered_by
from hinoki.utils import utc_now


class Member(Model, table="tracker_members"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str = field(max_length=80)
    token: str = field(unique=True)


@indexed("owner_id", "created_at")
@ordered_by("-created_at", "-id")
class Task(Model, table="tracker_tasks", label="Task", plural="Tasks"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    owner_id: UUID = foreign_key(Member, on_delete=OnDelete.RESTRICT, related_name="tasks")
    title: str = field(max_length=120)
    done: bool = False
    created_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None
```

* `table=` is a class keyword because it is needed when the class is created.
* `@indexed` and `@ordered_by` are decorators that state facts about the model below them ([model options](../model-options.md)). The default order applies to every query that does not call `order_by`.
* `foreign_key(Member, ...)` is a column holding the owner's id; the relation (`task.owner`) is never fetched by accident.

## The migration

The Tracker example already includes the migrations for this model and its index. Inspect the checked-in SQL and apply it to a disposable database:

```sh
hinoki migrations --dir examples/tracker/migrations status
hinoki migrations --dir examples/tracker/migrations sql 0001-initial
hinoki migrations --dir examples/tracker/migrations sql 0002-indexes
hinoki migrations --dir examples/tracker/migrations apply
```

```sql
CREATE TABLE "tracker_tasks" ( ... );
CREATE INDEX "ix_tracker_tasks_owner_id_created_at" ON "tracker_tasks" ("owner_id", "created_at");
```

This checked-in Tracker example has no `hinoki.toml`, so its commands use the explicit legacy `--dir` and `--models` targets. When you change a model, `hinoki migrations --dir examples/tracker/migrations plan --models examples.tracker.models` compares declarations with the recorded state and writes the next migration; the migration name is derived automatically, and `--name` can override its slug. Review the SQL before applying it; migrations never run at app startup. Destructive changes are flagged and need `--allow-destructive`. [Chapter 8](8-projects.md) uses the manifest-based workflow created by `hinoki new`.

Next: [2. Schemas](2-schemas.md).
