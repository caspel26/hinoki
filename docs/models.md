---
status: implemented
related:
  - relations.md
  - migrations.md
  - model-options.md
---

# Models and queries

`Model` maps a Python class to one PostgreSQL table. Its annotated fields define the columns; queries are explicit and do not load relations implicitly.

## Define a model

```python
from datetime import datetime
from uuid import UUID, uuid4

from hinoki.orm import Model, field, foreign_key, indexed, ordered_by, unique
from hinoki.utils import utc_now


class Member(Model, table="members"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    email: str = field(unique=True, max_length=254)


@indexed("owner_id", "created_at")
@ordered_by("-created_at", "-id")
class Task(Model, table="tasks", label="Task", plural="Tasks"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    owner_id: UUID = foreign_key(Member, on_delete="restrict", related_name="tasks")
    title: str = field(max_length=120)
    done: bool = False
    created_at: datetime = field(default_factory=utc_now)
```

`table=` is required. A model has exactly one primary key. Fields are keyword-only when constructing a model. Single-column uniqueness is `field(unique=True)`; use `@unique("a", "b")` for a composite constraint. There is no nested `class Meta`.

Supported field metadata includes primary key, uniqueness, maximum string length, defaults and foreign-key behavior. Database types are inferred from annotations; unsupported types fail during model or migration validation.

## Query rows

Class methods and querysets offer sync and async variants for common operations. Async variants are preferred in async handlers:

```python
task = await Task.aget(id=task_id)
tasks = await Task.all().filter(owner_id=member_id, done=False).order_by("title")
page = await Task.all().filter(done=False).apaginate(PageParams(page=1, page_size=20))
```

Filters accept validated field names and supported comparison/membership operators. Values are bound parameters; SQL identifiers are compiled from model metadata. Query methods do not issue SQL until an evaluation method such as awaiting the query, `aget`, `acount`, `aupdate` or `adelete` is called.

`@ordered_by` supplies ordering only when a query does not specify `order_by`. An explicit order replaces it. Counts and bulk updates do not inherit default ordering.

## Create and update

```python
task = await Task.acreate(TaskInput(title="Write docs"), owner_id=member_id)
task.done = True
await task.asave()  # updates changed columns
```

Server-supplied keywords take precedence over values from a `Schema`. A loaded model remembers its stored values; `asave()` updates changed columns and refuses primary-key changes. For a conditional write, use `aupdate_where` so ownership and state checks are part of the SQL condition.

The model constructor is statically typed. Some keyword-based model methods still have broader type-checker signatures; see [typing](typing.md).

## Relations

Declare a foreign-key column with `foreign_key(OtherModel, ...)`. It creates the `other_id` column and a named relation (`row.other`), but reading the relation does not fetch data. Load it explicitly with `select_related` or `prefetch_related`. For many-to-many data, declare an association model with two foreign keys and a composite `@unique` constraint; see [relations and loading](relations.md).

## Schema options

Use [model option decorators](model-options.md) for indexes, ordering and composite uniqueness. Use [schemas](schemas.md) to define request and response data; a model does not decide which columns cross the HTTP boundary.

## Migrations

Model changes do not alter a database. Generate and review migration files, then apply them deliberately with the [migration commands](migrations.md).
