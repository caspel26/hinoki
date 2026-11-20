---
status: implemented
related:
  - models.md
  - model-options.md
  - migrations.md
---

# 1. Models

A model declares a table. A relation is loaded only when you ask for it, never
as a side effect of reading an attribute.

```python
# tracker/models.py
from datetime import datetime
from uuid import UUID, uuid4

from hinoki import db
from hinoki.orm import Model, OnDelete, field, foreign_key, pk
from hinoki.utils import utc_now


@db.table("tracker_members")
class Member(Model):
    id: UUID = pk(default_factory=uuid4)
    name: str = field(max_length=80)
    token: str = field(unique=True)


@db.indexed("owner_id", "created_at")
@db.ordered_by("-created_at", "-id")
@db.table("tracker_tasks")
class Task(Model, label="Task", plural="Tasks"):
    id: UUID = pk(default_factory=uuid4)
    owner_id: UUID = foreign_key(
        Member, on_delete=OnDelete.RESTRICT, related_name="tasks"
    )
    title: str = field(max_length=120)
    done: bool = False
    created_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None
```

- `pk(...)` declares the primary key; `field(...)` adds column options such as
  `max_length` and `unique`.
- `@db.indexed("owner_id", "created_at")` adds a composite index.
  `@db.ordered_by(...)` sets the order of every query that does not call
  `order_by`.
- `foreign_key(Member, ...)` stores the owner's id. `task.owner` is fetched only
  when a query asks for it.
- `@db.table(...)` pins the table name, because a Tracker rule requires it
  ([chapter 5](5-rules.md)). Without it, Hinoki derives `<app>_<models>`, as
  [chapter 8](8-projects.md) shows.

## Apply the migrations

A migration is a SQL file generated from the models and checked in beside them.
Tracker ships two; the second adds the composite index:

```sql
-- tracker/migrations/0002-indexes.sql
-- index tracker_tasks (owner_id, created_at)
CREATE INDEX "ix_tracker_tasks_owner_id_created_at" ON "tracker_tasks" ("owner_id", "created_at");
```

From `examples/`, list and apply them:

```sh
uv run hinoki migrations status tracker
uv run hinoki migrations apply tracker
```

```text
database: sqlite:///.../examples/.hinoki/local.sqlite3 (from local SQLite default)
tracker          pending  0001-initial
tracker          pending  0002-indexes
database: sqlite:///.../examples/.hinoki/local.sqlite3 (from local SQLite default)
tracker: 0001-initial, 0002-indexes
```

Each command first names the database it uses. With no database configured,
that is `examples/.hinoki/local.sqlite3`.
Migrations run only when you call `apply`; an application never migrates at
startup.

When a model changes, `uv run hinoki migrations plan tracker` writes the next
migration. Review its SQL, then apply it.

## Recap

- A model is a typed class; decorators state table facts such as indexes and order.
- Migrations are reviewed files, applied by an explicit command.
- Relations are never loaded implicitly.

Next: [2. Schemas](2-schemas.md).
