# Query and write rows

Queries are explicit: nothing runs until you evaluate a query, values are bound
parameters, and relations load only when you ask. Examples use the Billing
models from [Define models](https://hinoki.caspel.dev/dev/models.md).

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
    projected = await Invoice.filter(paid=False).order_by("reference").aproject(InvoiceOut)
    one_projected = await Invoice.aget_as(InvoiceOut, id=invoice_id)
    maybe_projected = await Invoice.aget_or_none_as(InvoiceOut, reference="INV-7")
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
    projected = Invoice.filter(paid=False).order_by("reference").project(InvoiceOut)
    one_projected = Invoice.get_as(InvoiceOut, id=invoice_id)
    maybe_projected = Invoice.get_or_none_as(InvoiceOut, reference="INV-7")
    ```

`PageParams` comes from `hinoki.pagination`. `aget` and `aget_as` raise
`Invoice.NotFound` (a 404 in a request) or `MultipleObjectsReturned`. Building a
query (`filter`, `order_by`, `limit`, `offset`, `select_related`,
`prefetch_related`) issues no SQL; awaiting it or calling an evaluation method
does. `aproject` / `project`, `apaginate_as` / `paginate_as`, `aget_as` /
`get_as`, and `aget_or_none_as` / `get_or_none_as` encode eligible scalar,
`select_related`, and 1-level `prefetch_related` (reverse foreign keys and
many-to-many) schemas directly from Rust rows, materializing Python `Schema`
instances lazily from the owned rows if inspected and falling back transparently
when hooks, computed fields, validators, or scoped prefetches are present. A projected item passes `isinstance`, `dataclasses.is_dataclass`, `dataclasses.asdict`, `dataclasses.replace` and `copy.replace` as the `Schema` itself. `dataclasses.fields(type(item))` sees no fields (the class of a projected item is not the `Schema`); use `dataclasses.fields(item)`.

## Query methods

Every read and write entry point has an async form (`a*`) for `async def`
handlers and a matching sync form for `def` handlers.

### Starting a query on `Model`

| Method | Returns | Behavior |
| --- | --- | --- |
| `Model.all()` | `QuerySet[M]` | All rows (live rows only on `@db.soft_delete()` models). |
| `Model.filter(**conditions)` | `QuerySet[M]` | Shortcut for `Model.all().filter(**conditions)`. |
| `Model.with_deleted()` | `QuerySet[M]` | Includes soft-deleted rows on `@db.soft_delete()` models. |
| `Model.only_deleted()` | `QuerySet[M]` | Only soft-deleted rows on `@db.soft_delete()` models. |
| `Model.aget(**conditions)` / `Model.get(...)` | `M` | Fetch exactly one row; raises `Model.NotFound` or `MultipleObjectsReturned`. |
| `Model.aget_or_none(**conditions)` / `Model.get_or_none(...)` | `M \| None` | Fetch one row or `None`; still raises `MultipleObjectsReturned` if more than one matches. |

### Chainable `QuerySet` methods

These methods return a new `QuerySet` without touching the database.

| Method | Behavior |
| --- | --- |
| `.filter(**conditions)` | Add one or more `AND` conditions (`field=value` or `field__op=value`). |
| `.order_by(*names)` | Sort ascending (`"issued_at"`) or descending (`"-issued_at"`). Overrides `@db.ordered_by`. |
| `.limit(count)` | Cap the number of returned rows. |
| `.offset(count)` | Skip the first `count` rows. |
| `.select_related(*names)` | Load forward foreign-key relations in the same query (`JOIN`). See [Relations](https://hinoki.caspel.dev/dev/relations.md). |
| `.prefetch_related(*names, **scoped)` | Load forward, reverse, or many-to-many relations in bounded follow-up queries. Many-to-many links accept scoped querysets (`prefetch_related(tags=Tag.filter(...))`). |

### Evaluating a `QuerySet` (reads)

| Async | Sync | Returns | Behavior |
| --- | --- | --- | --- |
| `await qs` | `qs.list()` | `list[M]` | Fetch all matching model instances. |
| `await qs.afirst()` | `qs.first()` | `M \| None` | First matching row, or `None`. |
| `await qs.aget()` | `qs.get()` | `M` | Exactly one matching row; raises `Model.NotFound` or `MultipleObjectsReturned`. |
| `await qs.acount()` | `qs.count()` | `int` | Count matching rows (ignores `order_by`). |
| `await qs.aexists()` | `qs.exists()` | `bool` | `True` if at least one row matches. |
| `await qs.aproject(Schema)` | `qs.project(Schema)` | `list[S]` | Project matching rows to a `Schema` via the lazy Rust row path when eligible. |
| `await qs.apaginate(params=None, *, concurrent=False)` | `qs.paginate(...)` | `Page[M]` | Offset page plus `total`. `concurrent=True` runs rows and count concurrently outside a transaction. |
| `await qs.apaginate_as(Schema, params=None, *, concurrent=False)` | `qs.paginate_as(...)` | `Page[S]` | Offset page projected to `Schema` via the lazy Rust row path. |
| `await qs.apaginate_cursor(params)` | `qs.paginate_cursor(params)` | `CursorPage[M]` | Keyset page using a signed cursor, with no `COUNT` query. |

* `apaginate` runs the count and the page as two statements; concurrent writes
  can make `total` and `items` disagree slightly. `apaginate_cursor` runs one
  statement and no count; see [cursor pagination](https://hinoki.caspel.dev/dev/pagination.md#cursor-keyset-pagination).
* `@db.ordered_by` applies only when a query has no `order_by`. Counts and bulk
  updates ignore it.

### Write and soft-delete methods

| Async | Sync | Target | Behavior |
| --- | --- | --- | --- |
| `Model.acreate(data=None, /, *, returning=True, **values)` | `Model.create(...)` | `Model` | Insert one row from a `Schema` and/or server keywords. |
| `row.asave()` | `row.save()` | instance | Persist changed columns on an existing instance. |
| `row.aupdate_from(data=None, /, **values)` | `row.update_from(...)` | instance | Apply a `Schema` / keywords and save changed columns. |
| `Model.aupdate(*, returning=True, **values)` | `Model.update(...)` | `Model` | Update one row by primary key; raises `NotFound` if missing (`returning=True`). |
| `Model.aupdate_where(conditions, values, *, returning=True)` | `Model.update_where(...)` | `Model` | Conditional `UPDATE` matching `conditions` (supports `field__op`). |
| `qs.aupdate(values, *, returning=True)` | `qs.update(...)` | `QuerySet` | Update all matching rows (refuses `limit`/`offset`). |
| `Model.adelete(**conditions)` / `qs.adelete()` | `delete(...)` | `Model` / `QuerySet` | Delete matching rows (sets delete timestamp on `@db.soft_delete()` models). |
| `row.asoft_delete()` | `row.soft_delete()` | instance | Soft-delete one `@db.soft_delete()` instance. |
| `row.arestore()` / `qs.arestore()` | `restore()` | instance / `QuerySet` | Clear the soft-delete timestamp (`Model.only_deleted().filter(...).arestore()`). |
| `row.ahard_delete()` / `qs.ahard_delete()` | `hard_delete()` | instance / `QuerySet` | Physical SQL `DELETE` on a `@db.soft_delete()` model. |
| `Model.abulk_create(items, *, returning=True)` | `Model.bulk_create(...)` | `Model` | Insert many `Schema` objects or dicts in one transaction/savepoint. |
| `Model.abulk_update(instances)` | `Model.bulk_update(...)` | `Model` | Save changed instances in one transaction/savepoint. |
| `Model.abulk_delete(items)` | `Model.bulk_delete(...)` | `Model` | Delete instances or primary keys in chunks of up to 10 000. |

## Filter operators

`filter(field=value)` and `filter(field__op=value)` accept model field names and
a fixed set of lookup operators compiled in Rust. Every condition in a
`filter(...)` call (or chained `.filter(...).filter(...)`) is combined with
`AND`, values are bound parameters, and SQL identifiers come from model
metadata.

| Lookup | Example | Compiled SQL | Notes |
| --- | --- | --- | --- |
| *(exact)* | `paid=False` | `paid = $1` | Passing `None` (`voided_at=None`) compiles to `IS NULL`. |
| `ne` | `status__ne="draft"` | `status <> $1` | Passing `None` compiles to `IS NOT NULL`. For non-`None` values, `NULL` rows do not match. |
| `gt` | `total__gt=100` | `total > $1` | Rejects `None` (`FieldError`). |
| `gte` | `total__gte=100` | `total >= $1` | Rejects `None` (`FieldError`). |
| `lt` | `total__lt=500` | `total < $1` | Rejects `None` (`FieldError`). |
| `lte` | `total__lte=500` | `total <= $1` | Rejects `None` (`FieldError`). |
| `in` | `status__in=["open", "overdue"]` | `status IN ($1, $2)` | Takes a sequence/set; rejects `str` and `bytes` (`FieldError`) so strings are never split by character. |
| `isnull` | `voided_at__isnull=True` | `voided_at IS NULL` | `False` compiles to `IS NOT NULL`. |
| `contains` | `reference__contains="inv-"` | case-insensitive `LIKE` | Escapes `%`, `_` and `\` literally. Rejected on `Decimal` columns (`FieldError`). |

* `field__ne=value` compiles to `<>`: rows where the column is `NULL` are not
  returned. Add an `isnull` filter when they matter.
* `contains` matches text case-insensitively and treats `%`, `_` and `\`
  literally. SQLite folds ASCII case only; MySQL and MariaDB assume the
  `NO_BACKSLASH_ESCAPES` mode is off (the default).
* Passing `None` with any operator other than exact equality or `ne` raises
  `FieldError`.

### Filtering across relations

* **Forward foreign keys**: traverse a foreign-key relation with `__` and any
  operator above:

  ```python
  invoices = await Invoice.filter(customer__name__contains="acme", paid=False)
  ```

  Hinoki adds the required `JOIN` automatically and matches only live rows when
  the target model uses `@db.soft_delete()`.
* **Many-to-many links**: filter on a target field (`tags__slug="rust"`) or
  test whether any live link exists (`tags__isnull=True` / `False`):

  ```python
  rust_posts = await Post.filter(tags__slug="rust")
  untagged = await Post.filter(tags__isnull=True)
  ```

  Many-to-many filters compile to a subquery against the bridge table and ignore
  soft-deleted targets. `<link>__isnull` cannot be combined with other
  conditions on the same link.
* **Reverse foreign keys**: filtering from the parent across a reverse relation
  raises `FieldError`; query the child model directly instead.

### Client query filters (`FilterSet`)

For generated CRUD list endpoints, declare accepted HTTP query parameters with
`FilterSet` from `hinoki.filters` instead of passing raw query parameters to
`filter()`. Only declared fields and shapes are accepted; validation and query
compilation run in Rust.

| Declaration | Query string | Behavior |
| --- | --- | --- |
| `paid: Eq[bool]` | `?paid=false` | Exact match (`=`). |
| `status: In[Literal["open", "paid"]]` | `?status=open,paid` | Membership (`IN`, up to 50 comma-separated values). |
| `issued: Range[date]` | `?issued.from=2026-01-01&issued.to=2026-01-31` | Inclusive bounds (`>=` / `<=`); either end is optional (`Bounds[T]`). |
| `search = Search("reference", "notes")` | `?search=inv` | Case-insensitive `contains` matching any listed text column. |
| `ordering = ("issued", "reference")` | `?ordering=-issued` | Allowed sort columns; prefix `-` for descending. |

Supported scalar types for `Eq`, `In`, and `Range` are `bool`, `int`, `float`,
`str`, `Literal["..."]`, `UUID`, `date`, `datetime`, and `Decimal` (optionally
nullable with `| None`). See [Generate CRUD routes](https://hinoki.caspel.dev/dev/crud.md) for full `FilterSet`
usage.

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
| row order | `@db.ordered_by(...)` ([model options](https://hinoki.caspel.dev/dev/model-options.md)) |
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

- [Relations](https://hinoki.caspel.dev/dev/relations.md): `select_related` and `prefetch_related`
- [Generate CRUD routes](https://hinoki.caspel.dev/dev/crud.md): `FilterSet` client query parameters
- [Model options](https://hinoki.caspel.dev/dev/model-options.md): `@db.soft_delete()`, `@db.ordered_by()`, `@db.timestamps()`
- [Lifecycle hooks](https://hinoki.caspel.dev/dev/model-hooks.md)
- [Transactions](https://hinoki.caspel.dev/dev/transactions.md)
- [Typing](https://hinoki.caspel.dev/dev/typing.md): what type checkers see for keyword-based methods
