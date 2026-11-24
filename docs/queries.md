---
status: implemented
related:
  - models.md
  - relations.md
  - transactions.md
  - pagination.md
---

# Query and write rows

Queries are explicit: nothing runs until you evaluate a query, values are bound
parameters, and relations load only when you ask. Examples use the Billing
models from [Define models](models.md).

## Query rows

=== "Async"

    ```python
    # inside an async operation or handler
    invoice = await Invoice.aget(id=invoice_id)
    maybe = await Invoice.aget_or_none(reference="INV-7")
    unpaid = await Invoice.filter(customer_id=customer.id, paid=False).order_by("reference")
    first = await Invoice.filter(paid=False).afirst()
    count = await Invoice.filter(paid=False).acount()
    page = await Invoice.all().apaginate(PageParams(page=1, page_size=20))
    ```

=== "Sync"

    ```python
    # inside a sync handler (runs in the bounded worker pool)
    invoice = Invoice.get(id=invoice_id)
    maybe = Invoice.get_or_none(reference="INV-7")
    unpaid = (
        Invoice.filter(customer_id=customer.id, paid=False).order_by("reference").list()
    )
    first = Invoice.filter(paid=False).first()
    count = Invoice.filter(paid=False).count()
    page = Invoice.all().paginate(PageParams(page=1, page_size=20))
    ```

`PageParams` comes from `hinoki.pagination`. `aget` raises `Invoice.NotFound`
(a 404 in a request) or `MultipleObjectsReturned`. Building a query
(`filter`, `order_by`, `limit`, `offset`, `select_related`) issues no SQL;
awaiting it or calling an evaluation method does.

### Filters

`filter(field=value)` and `filter(field__op=value)` accept model field names and
the supported comparison and membership operators (`ne`, `in`, `isnull`, `gt`,
`gte`, `lt`, `lte`, `contains`). SQL identifiers come from model metadata.

* `field__ne=value` compiles to `<>`: rows where the column is `NULL` are not
  returned. Add an `isnull` filter when they matter.
* `contains` matches text case-insensitively and treats `%`, `_` and `\`
  literally. SQLite folds ASCII case only; MySQL and MariaDB assume the
  `NO_BACKSLASH_ESCAPES` mode is off (the default).
* `apaginate` runs the count and the page as two statements; concurrent writes
  can make `total` and `items` disagree slightly. `apaginate_cursor` runs one
  statement and no count; see [cursor pagination](pagination.md#cursor-keyset-pagination).
* `@db.ordered_by` applies only when a query has no `order_by`. Counts and bulk
  updates ignore it.

## Reusable queries

A reusable query is a typed class method returning a `QuerySet`. There is no
hidden manager: `Model.all()` means every live row on a soft-delete model and
every row on an ordinary model.

```python
# notes/apps/billing/models.py (continued)
from typing import Self

from hinoki.orm import QuerySet


class Invoice(Model):
    ...  # fields as in Define models
    voided_at: datetime | None = None

    @classmethod
    def open(cls) -> QuerySet[Self]:
        return cls.filter(voided_at=None, paid=False)
```

Pass it where the framework asks for a base query, so every operation sees the
same rows:

```python
# notes/apps/billing/api.py
from typing import Any

from hinoki import API, Caller, crud
from hinoki.admin import ModelAdmin
from hinoki.filters import Eq, FilterSet
from hinoki.orm import QuerySet

from notes.security import billing_staff

from .models import Invoice
from .schemas import InvoiceOut


class InvoiceFilters(FilterSet):
    paid: Eq[bool]


@crud(
    Invoice,
    path="/invoices",
    read=InvoiceOut,
    access=billing_staff,
    filters=InvoiceFilters,
)
class Invoices(API):
    def get_queryset(self, principal: Caller[Any] | None) -> QuerySet[Invoice]:
        return Invoice.open()


class InvoiceAdmin(ModelAdmin[Invoice]):
    def queryset(self, principal: Caller[Any]) -> QuerySet[Invoice]:
        return Invoice.open()
```

| Rule | Where |
| --- | --- |
| row order | `@db.ordered_by(...)` ([model options](model-options.md)) |
| an app-wide policy, such as hiding voided rows | a named class method, used in `get_queryset()` and `ModelAdmin.queryset()` |
| rows that depend on the caller | `get_queryset(principal)` or `ModelAdmin.queryset(principal)` |

!!! warning "Not a security boundary"
    `Model.all()` and raw SQL skip a reusable query. Keep ownership and
    permissions in `access=`, `owner=` and `ModelAdmin.can()`.

## Create and update

```python
# inside an async operation
invoice = await Invoice.acreate(InvoiceIn(reference="INV-8"), customer_id=customer.id)
invoice.paid = True
await invoice.asave()  # updates the changed columns
await invoice.aupdate_from(InvoicePatch(paid=False))
changed = await Invoice.aupdate_where({"id": invoice.id, "paid": False}, {"paid": True})
```

* Keywords set by the server win over values from a `Schema`; a `Schema` never
  carries the primary key.
* `asave()` writes changed columns and refuses a primary-key change.
* `aupdate_where(conditions, values)` puts ownership and state checks in the
  SQL condition: use it for conditional writes instead of read-then-write.
* `QuerySet.aupdate(values)` and `adelete()` act on every matching row, ignore
  ordering and refuse a query with `limit` or `offset`; select the keys first
  and filter on `id__in=[...]`.

On a model decorated with `@db.soft_delete()`, `adelete()` / `delete()` sets the
delete timestamp with a scoped `UPDATE`; use `arestore()` / `restore()` to clear
it, and `ahard_delete()` / `hard_delete()` for a physical `DELETE`. Model
queries are live-only by default; `Model.with_deleted()` and
`Model.only_deleted()` are explicit class-level scopes. Relations use the live
scope too: reverse relations, many-to-many links, and forward
`select_related`/`prefetch_related`, where a referenced soft-deleted row reads
as `None`. A filter across a relation (`parent__name=...`) joins live rows
only.

For `@db.track_status()`, instance saves bump `changed_at` only when the value
actually changes. Bulk `QuerySet.aupdate()` cannot compare stored values and
bumps the timestamp whenever the tracked field appears in `values`, including
when it equals the stored value.

## Writing many rows at once

`abulk_create`, `abulk_update` and `abulk_delete` (sync `bulk_*`) each run in
one transaction, or a savepoint inside an open one:

```python
# inside an async operation
rows = await Invoice.abulk_create(
    [
        {"reference": "A-1", "customer_id": customer.id},
        {"reference": "A-2", "customer_id": customer.id},
    ]
)
rows[0].paid = True
await Invoice.abulk_update(rows)  # saves changed rows only
deleted = await Invoice.abulk_delete([rows[0], rows[1].id])  # instances or keys
```

* `abulk_create` takes `Schema` objects or dicts, each carrying every required
  value, and returns the rows in order; defaults, factories, validation and
  hooks apply as for `acreate`. It takes no server keywords: put them in each
  item.
* `abulk_update` refuses an unsaved instance and returns every instance it
  was given, unchanged ones included; `abulk_delete` returns how many
  rows existed, deletes in chunks of at most 10 000 keys and runs no hooks.
* One statement per row (per chunk for deletes): these give atomicity, not a
  batching speed-up.

## Writes that do not read the row back

By default a write returns the stored row (`RETURNING` on PostgreSQL and SQLite,
a follow-up `SELECT` in the same transaction on MySQL and MariaDB).
`returning=False` skips that:

```python
# inside an async operation
draft = await Invoice.acreate(reference="D-1", customer_id=customer.id, returning=False)
count = await Invoice.aupdate(id=draft.id, paid=True, returning=False)
count = await Invoice.filter(customer_id=customer.id).aupdate(
    {"paid": False}, returning=False
)
```

| Method | Default | `returning=False` |
| --- | --- | --- |
| `acreate`, `insert` | the stored row | the same instance, holding the sent values |
| `aupdate` | the stored row; `NotFound` when nothing matches | `int` row count; `0`, no `NotFound` |
| `aupdate_where`, `QuerySet.aupdate` | `list` of stored rows | `int` row count |

Nothing is re-read: database-side changes (timestamp precision, numeric scale,
JSON normalization, triggers) are not reflected; read with `aget` when you need
them. `@db.timestamps()` and validation still run. PostgreSQL and SQLite count
matched rows; MySQL and MariaDB count rows whose values changed. A model cannot
declare a field named `returning`.

## See also

- [Relations](relations.md): `select_related` and `prefetch_related`
- [Lifecycle hooks](model-hooks.md)
- [Transactions](transactions.md)
- [Typing](typing.md): what type checkers see for keyword-based methods
