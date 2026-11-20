---
status: implemented
related:
  - models.md
  - migrations.md
  - utilities.md
---

# Model options

Options that describe a model beyond its columns are decorators, read top to
bottom like action decorators. There is no nested `class Meta`; only names stay
class keywords.

```python
# notes/apps/billing/models.py
from datetime import datetime
from uuid import UUID, uuid4

from hinoki import db
from hinoki.orm import Model, OnDelete, field, foreign_key, pk, timestamps, unique
from hinoki.utils import utc_now

from .customers import Customer


@unique("customer_id", "reference")
@db.indexed("customer_id", "created_at")
@db.ordered_by("-created_at", "-id")
@timestamps()
class Quote(Model, label="Quote", plural="Quotes"):
    id: UUID = pk(default_factory=uuid4)
    customer_id: UUID = foreign_key(
        Customer, on_delete=OnDelete.RESTRICT, related_name="quotes"
    )
    reference: str = field(max_length=40)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
```

| Option | Form | Effect |
| --- | --- | --- |
| single-column index | `field(index=True)` | `CREATE INDEX` on one column |
| composite or foreign-key index | `@db.indexed("a", "b")` | `CREATE INDEX` on the columns, in order |
| composite uniqueness | `@unique("a", "b")` | `UNIQUE (a, b)`; one column uses `field(unique=True)` |
| default order | `@db.ordered_by("-created_at", "id")` | order of queries without `order_by`; `-` is descending |
| update timestamp | `@timestamps()` | refreshes `updated_at` on every ORM update |
| table name | `@db.table("quotes")` (optional) | replaces the derived `<app>_<models>` name |
| app part of the name | `app="sales"` (class keyword, optional) | replaces `<app>`; ignored with an explicit table |
| display names | `label=`, `plural=` (class keywords, optional) | replace the derived singular and plural |

`unique` and `timestamps` are imported from `hinoki.orm`; `indexed`,
`ordered_by` and `table` from `hinoki.db` (the same objects as in
`hinoki.orm`).

## Indexes

Use `field(index=True)` for one ordinary column and `@db.indexed(...)` for a
composite index. A `foreign_key` has no `index=` option: index it with
`@db.indexed("customer_id")`, or with a composite index that starts with it.
The optional `HNK-DB-002` rule (`Models.require_foreign_key_index`) checks this.

* `index=` takes `True` or `False`; a primary key or unique column is already
  indexed and refuses `index=True`.
* `field(index=True)` and `@db.indexed("column")` on the same column produce
  one index.
* Names are `ix_<table>_<columns>_<digest>`, cut to the backend limit (63 bytes
  on PostgreSQL, 64 on MySQL and MariaDB); the digest keeps
  `indexed("a_b")` and `indexed("a", "b")` apart.
* Adding or removing an index is a non-destructive migration.

## Default order

`@db.ordered_by(...)` orders every query that has no `order_by`, lists and
pages included. An explicit `order_by` replaces it; counts and bulk updates
ignore it. End with a unique column (`"-id"`) so pages are stable.

## Update timestamps

`@timestamps()` sets a declared, non-nullable `updated_at: datetime` on every
ORM update statement. `@timestamps("modified_at", clock=my_clock)` names another
column or injects a clock. See [utilities](utilities.md#model-update-timestamps).

## Names

Leave the table name derived for new models. Pin it with `@db.table(...)` when
the model has migration history and its class or module may change: a changed
derived name is planned as a guarded drop and create, never a rename. Naming
the table twice with different names (`@db.table` twice, or with `table=`) is
an error naming both. Use `label=`/`plural=` for irregular or localized nouns.
See [table and display names](models.md#table-and-display-names).

## Rules for all options

* Column names are checked when the decorator runs; a typo fails at import with
  the model and the unknown names.
* Apply options before the model is used: the migration planner, the query
  layer and the admin read them from the class.
* Options of [abstract bases](models.md#share-fields-abstract-models-and-mixins)
  are inherited; a decorator on the subclass replaces that kind of option. A
  table model does not pass its options to a subclass.

!!! note "Not yet"
    Partial and expression indexes, and soft delete, are not available.

## See also

- [Define models](models.md)
- [Migrations](migrations.md)
