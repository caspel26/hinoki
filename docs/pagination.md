---
status: implemented
---

# Pagination

An action opts into page-number pagination with `pagination=PageNumberPagination()`, or into [cursor pagination](#cursor-keyset-pagination) with `pagination=CursorPagination()`. A size you leave out comes from the application's `pagination` settings group; without settings the defaults are `page_size=20` and `max_page_size=100`. The framework parses and validates `page` and `page_size`, injects a `PageParams` value, and documents both query parameters in OpenAPI.

The Billing app lists invoices a page at a time. The handler asks the model for one page, and `Page.map()` turns its rows into `Schema` instances:

```python
# notes/apps/billing/api.py
from hinoki import RouteGroup
from hinoki.pagination import Page, PageNumberPagination, PageParams

from notes.security import billing_staff

from .models import Invoice
from .schemas import InvoiceOut

invoices = RouteGroup("/invoices", access=billing_staff, atomic=True)


@invoices.get("/", pagination=PageNumberPagination())
async def list_invoices(page: PageParams) -> Page[InvoiceOut]:
    return (await Invoice.all().apaginate(page)).map(InvoiceOut.from_model)
```

Pages are one-based. `page_size` defaults to the strategy's configured size and cannot exceed its maximum. Invalid values produce a validation response before the handler runs; `page` is limited to 1 000 000 000, and longer or larger values answer `422` with the code `le`.

`Page` contains `items`, `page`, `page_size`, `total` and the computed `pages`. Empty result sets have zero pages; a page beyond the end has an empty `items` list. `Page.map()` converts each item while preserving the counts and requested page.

## Defaults for the whole application

`settings.AppSettings` has a `pagination` group, set in code, in the environment or in TOML. With the project's settings class:

```python
# notes/settings.py
from hinoki import settings


class ProjectSettings(settings.AppSettings, prefix="NOTES_"):
    pass
```

```sh
export NOTES_PAGINATION__PAGE_SIZE=25
export NOTES_PAGINATION__MAX_PAGE_SIZE=200
```

The application in `notes/main.py` reads them through `Hinoki(settings=ProjectSettings, ...)`.

Precedence, from strongest to weakest: a size set on the action's `PageNumberPagination(...)`, the application's `pagination` settings, then 20 and 100. Each size is resolved separately, so `PageNumberPagination(page_size=10)` keeps its 10 and takes its maximum from the settings. The result must satisfy `1 <= page_size <= max_page_size`: invalid settings fail when they load, and an action whose own size does not fit the resolved other one (for example `PageNumberPagination(max_page_size=10)` under a settings `page_size` of 25) fails when it is included, naming the action. The generated OpenAPI parameters show the resolved values.

Use the queryset's `apaginate`/`paginate` methods for database rows. They append the primary key as the last sort key (after any `order_by`), so rows with equal sort values keep one position across pages. `Page.of(rows, params)` slices an in-memory sequence and counts that sequence; it is not a substitute for database pagination on large tables.

### Concurrent count and rows

Pass `concurrent=True` to run the `COUNT(*)` and page-row queries together on PostgreSQL or MySQL pools with `max_size >= 2`:

```python
@invoices.get("/fast", pagination=PageNumberPagination(), atomic=False)
async def list_invoices_fast(page: PageParams) -> Page[InvoiceOut]:
    return (await Invoice.all().apaginate(page, concurrent=True)).map(
        InvoiceOut.from_model
    )
```

Because the two queries check out separate pool connections, they may observe slightly different snapshots under concurrent writes (the same trade-off as any non-transactional read). Inside an active transaction (`atomic=True` or `async with db.atomic():`), on SQLite, or when the pool has `max_size < 2`, `apaginate(..., concurrent=True)` automatically runs the count and row queries sequentially on one connection so transactions preserve read-your-writes and single-connection pools never deadlock.

### Direct schema projection

Use `apaginate_as` (or `paginate_as` in sync handlers) to project rows directly to a `Schema` in Rust without constructing intermediate Python `Model` or `Schema` objects on the response path:

```python
@invoices.get("/projected", pagination=PageNumberPagination())
async def list_invoices_projected(page: PageParams) -> Page[InvoiceOut]:
    return await Invoice.all().apaginate_as(InvoiceOut, page, concurrent=True)
```

The returned `Page[InvoiceOut]` carries the pre-encoded JSON bytes and owned rows. Returning it from a handler emits the exact same JSON as `(await Invoice.all().apaginate(page)).map(InvoiceOut.from_model)` and passes `validate_response=True` checks. Accessing `.items`, calling `.map()`, or inspecting elements materializes the `InvoiceOut` instances from the owned rows without issuing a second database query. Scalar fields, `select_related`, and 1-level `prefetch_related` (reverse foreign keys and many-to-many) are encoded directly in Rust; schemas with computed fields, hooks, validators, custom `dump()`, or scoped prefetches fall back to normal Python hydration transparently.

## Cursor (keyset) pagination

`pagination=CursorPagination(...)` pages by position instead of by offset. A response carries `next` and `previous` cursors that clients should treat as opaque; the database starts each page at the cursor's row through the sort index, so a page costs the same at any depth, and no `COUNT` runs. Use it for long, growing lists or feeds; keep page numbers when users jump to page *n* or need a total.

```python
# notes/apps/billing/api.py
from hinoki.pagination import CursorPage, CursorPagination, CursorParams


@invoices.get("/feed", pagination=CursorPagination(page_size=20))
async def feed(page: CursorParams) -> CursorPage[InvoiceOut]:
    return (await Invoice.all().apaginate_cursor(page)).map(InvoiceOut.from_model)
```

```sh
export NOTES_PAGINATION__CURSOR_SECRET="$(openssl rand -hex 32)"
```

`GET /invoices/feed?limit=20` answers `{"items": [...], "next": "<cursor>", "previous": null, "page_size": 20}`. Pass `next` as `cursor` to read forward and `previous` to read back. `next` is `null` on the last page; `previous` is `null` on the first. Generated lists switch with `crud(..., pagination=CursorPagination())` and return the same envelope.

* **Ordering.** The page follows the query's `order_by` (or the model's `ordered_by`), with the primary key appended as the final tie-break, so rows with equal sort values are never skipped or repeated. Sort columns must be non-null `int`, `str`, `uuid`, `datetime` or `float` columns; anything else raises a `FieldError`, and a `crud(...)` whose model ordering is invalid fails at registration with a `RegistrationError` naming the model and column (an ordering added by a custom `get_queryset` is checked on each request). A `str` sort value is bounded: at most 1024 bytes per value, and a whole cursor at most 4096 characters, the same limit for issuing and reading. A page whose boundary row exceeds it, and whose link is needed, answers `422` with the field `cursor` and the code `unencodable` (it never answers `500`); order by a shorter column when values can be long. The queryset's own `limit` and `offset` are ignored.
* **Filters and scope.** The cursor holds a position. `get_queryset`, ownership and tenant policies are applied to every page exactly as for the first one, so a cursor cannot reach rows its holder could not list. On a generated list with [client filters](crud.md#filter-order-and-search-the-list), the cursor is also bound to the filters and the `ordering` that the client sent: a cursor used with different ones answers `422` with the code `invalid`. A list without client filters keeps its cursors valid as before. The `ordering` columns of the `FilterSet` must be non-null `int`, `str`, `uuid`, `datetime` or `float` columns, checked when the routes are declared.
* **Signing.** Cursors are signed with HMAC-SHA256 in Rust and bound to the table and the ordering they were issued for, so they are tamper-proof. They are not encrypted: a cursor is base64 and readable, and it contains the sort-key values of the boundary row. Do not order a cursor list by a column whose value must stay private, even when the read `Schema` hides it. The secret comes from `CursorPagination(secret=...)` or `pagination.cursor_secret` and must have at least 32 bytes; without one, including the action fails with a `RegistrationError`. Changing the secret invalidates outstanding cursors.
* **Expiry.** `cursor_ttl_seconds` (setting) or `CursorPagination(ttl=...)` makes a cursor expire; the default never does.
* **Errors.** A tampered, malformed, foreign or differently ordered cursor answers `422` with the field `cursor` and the code `invalid`; an expired one answers `422` with `expired`; an unencodable boundary value answers `422` with `unencodable`. `limit` follows the same bounds as `page_size`.
* **Rust path.** The keyset `WHERE`, the signature, the direction handling and, for a generated plain list, the JSON page are produced in Rust.
* **Indexes.** Index the sort columns in the sort order (for example `(rank DESC, id)`) to get the constant-time pages; without one the database sorts the table for every page.
* **Admin.** The admin lists keep page-number pagination.

The `pagination` settings group also takes `cursor_secret` and `cursor_ttl_seconds`; sizes resolve as described above, and `page_size`/`max_page_size` apply to `limit`. The measurements are in `benchmarks/README.md` (`cursorbench.py`).
