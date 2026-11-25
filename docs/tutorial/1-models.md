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

The Tracker keeps one model per app, each in the app's `models.py`. The members
app owns the table of people:

```python
# tracker/apps/members/models.py
from uuid import UUID, uuid4

from hinoki import db
from hinoki.orm import Model, field, pk


@db.table("tracker_members")
class Member(Model):
    id: UUID = pk(default_factory=uuid4)
    name: str = field(max_length=80)
    token_hash: str = field(unique=True)  # SHA-256 hex digest, never the token
```

The tasks app belongs to a member through a foreign key:

```python
# tracker/apps/tasks/models.py
from datetime import datetime
from uuid import UUID, uuid4

from hinoki import db
from hinoki.orm import Model, OnDelete, field, foreign_key, pk
from hinoki.utils import utc_now
from tracker.apps.members.models import Member


@db.indexed("owner_id", "created_at")
@db.ordered_by("-created_at", "-id")
@db.table("tracker_tasks")
class Task(Model, label="Task", plural="Tasks"):
    id: UUID = pk(default_factory=uuid4)
    owner_id: UUID = foreign_key(
        Member, on_delete=OnDelete.RESTRICT, related_name="tasks"
    )
    title: str = field(max_length=120)
    priority: int = 3
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
  when a query asks for it. Because `tasks` imports `Member`, its entry in
  `hinoki.toml` says `requires = ["members"]`.
- `@db.table(...)` pins the table name, because a Tracker rule requires it
  ([chapter 5](5-rules.md)). Without it, Hinoki derives `<app>_<models>`, as
  [chapter 8](8-projects.md) shows.

The third app, `activity`, has an `Activity` model of the same kind: a line per
task change. [Chapter 3](3-operations.md) shows how it gets written.

## Apply the migrations

A migration is a SQL file generated from the models and checked in beside them.
Each app has its own `migrations/` directory. Tasks ships one:

```sql
-- tracker/apps/tasks/migrations/0001-create-table-tracker-tasks.sql
-- hinoki migration 0001-create-table-tracker-tasks
-- generated from the models; review it.

-- create table tracker_tasks
CREATE TABLE "tracker_tasks" (
    "id" UUID PRIMARY KEY,
    "owner_id" UUID NOT NULL REFERENCES "tracker_members" ("id") ON DELETE RESTRICT,
    "title" varchar(120) NOT NULL,
    "priority" integer NOT NULL,
    "done" boolean NOT NULL,
    "created_at" timestamptz NOT NULL,
    "completed_at" timestamptz
);
CREATE INDEX "ix_tracker_tasks_owner_id_created_at_e46b5006" ON "tracker_tasks" ("owner_id", "created_at");
```

From `examples/tracker`, list and apply them:

```sh
uv run hinoki migrations status
uv run hinoki migrations apply
```

```text
database: sqlite:///…/examples/tracker/.hinoki/local.sqlite3 (from local SQLite default)
members          pending  0001-create-table-tracker-members
tasks            pending  0001-create-table-tracker-tasks
activity         pending  0001-create-table-tracker-activity
database: sqlite:///…/examples/tracker/.hinoki/local.sqlite3 (from local SQLite default)
members: 0001-create-table-tracker-members
tasks: 0001-create-table-tracker-tasks
activity: 0001-create-table-tracker-activity
```

Each command first names the database it uses. With no database configured,
that is `.hinoki/local.sqlite3` beside `hinoki.toml`. `apply` runs the apps in
`requires` order, so `tracker_members` exists before the table that references
it. Migrations run only when you call `apply`; an application never migrates at
startup.

When a model changes, `uv run hinoki migrations plan tasks` writes the next
migration of that app. Review its SQL, then apply it.

## Recap

- A model is a typed class; decorators state table facts such as indexes and order.
- Each app owns its models and migrations; `requires` orders them.
- Migrations are reviewed files, applied by an explicit command.
- Relations are never loaded implicitly.

Next: [2. Schemas](2-schemas.md).
