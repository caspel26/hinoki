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

The explicit `tracker_members` and `tracker_tasks` names above are pinned to
the example's checked-in migration history. New models can omit `table=`:

```python
class Invoice(Model):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    reference: str
```

That declaration derives the table `invoices`, the singular display name
`Invoice` and the plural `Invoices`. `label=` and `plural=` override the
display names; set `table=` explicitly for irregular/localized names and to
keep a physical table stable when a Python class is renamed. The fallback
pluralizer is deterministic and English-oriented, so use explicit names for
irregular nouns.

* `@indexed` and `@ordered_by` are decorators that state facts about the model below them ([model options](../model-options.md)). The default order applies to every query that does not call `order_by`.
* `foreign_key(Member, ...)` is a column holding the owner's id; the relation (`task.owner`) is never fetched by accident.

## The migration

The Tracker example already includes the migrations for this model and its index. Inspect the checked-in SQL and apply it to a disposable database:

```sh
cd examples
uv run --project .. hinoki migrations status
uv run --project .. hinoki migrations sql --app tracker 0001-initial
uv run --project .. hinoki migrations sql --app tracker 0002-indexes
uv run --project .. hinoki migrations apply --app tracker
```

```sql
CREATE TABLE "tracker_tasks" ( ... );
CREATE INDEX "ix_tracker_tasks_owner_id_created_at" ON "tracker_tasks" ("owner_id", "created_at");
```

The repository's `examples/hinoki.toml` registers Tracker alongside Notes and Blog, each with a separate model module and migration package. When you change a model, `hinoki migrations plan --app tracker` compares declarations with the recorded state and writes the next migration; the number and slug are derived automatically, and `--name` can override the slug. Review the SQL before applying it; migrations never run at app startup. Destructive changes are flagged and need `--allow-destructive`. [Chapter 8](8-projects.md) creates a new manifest-based project with `hinoki new` and adds apps with `hinoki app create`.

Next: [2. Schemas](2-schemas.md).
