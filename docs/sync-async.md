---
status: implemented
related:
  - request-lifecycle.md
  - transactions.md
  - settings.md
---

# Sync and async code

Each worker runs one event loop. `async def` is the native style; a plain `def` handler or hook is fully supported and runs on a bounded thread pool, with the request's context and blocking ORM twins that never block the loop.

Two actions added to the `invoices` group of [function endpoints](function-endpoints.md), answering with a one-field `Schema`:

```python
# notes/apps/billing/api.py  (in the invoices group's module)
from hinoki import Schema


class UnpaidCount(Schema):
    count: int


@invoices.get("/unpaid/count")
async def count_unpaid() -> UnpaidCount:  # async: await the a... method
    return UnpaidCount(count=await Invoice.filter(paid=False).acount())


@invoices.get("/unpaid/count-sync")
def count_unpaid_sync() -> UnpaidCount:  # sync: the twin without the "a"
    return UnpaidCount(count=Invoice.filter(paid=False).count())
```

The group carries the access policy and `atomic=True` for both.

A name with the `a` prefix awaits the database (`aget`, `acreate`, `acount`); the same name without it is the blocking twin. A queryset is lazy until evaluated: `await queryset` is the async form of `queryset.list()`.

## Choose a style

* Prefer `async def`. Use `def` for code that is naturally blocking: a library without async support, or CPU work measured to be short.
* Never call blocking code from an `async def`. From a `def`, use the sync twin instead of calling an `a...` coroutine.
* CPU-heavy work belongs in a separate process, not in either pool.
* A sync ORM call made on the event loop raises, naming the `a...` method to use.

## What runs where

| Surface | Async | Sync | Notes |
| --- | --- | --- | --- |
| Action handlers | `async def` | `def` | Sync runs on the executor |
| CRUD hooks (`get_queryset`, `written_queryset`, `to_schema`, `store`, `change`, `remove`) | `async def` | `def` | Same executor as handlers |
| Auth providers (`authenticate`, `resolve`, `resolve_token`, JWT `resolve_claims`) | `async def` | `def` | Sync hooks run on the executor |
| Model writes and lookups | `acreate`, `aget`, `aget_or_none`, `aupdate`, `aupdate_where`, `adelete`, `asave`, `aupdate_from`, `abulk_create`, `abulk_update`, `abulk_delete` | The same names without `a` | Sync calls work only inside a sync handler or hook served by Hinoki; both take `returning=False` ([queries](queries.md#writes-that-do-not-read-the-row-back)) |
| QuerySet reads | `await qs`, `afirst`, `aget`, `acount`, `aexists`, `apaginate` | `list`, `first`, `get`, `count`, `exists`, `paginate` | |
| QuerySet writes | `aupdate`, `adelete` | `update`, `delete` | |
| Relations | `select_related`, `prefetch_related` | same | Never lazy in either style |
| Raw SQL (`Database.fetch`, `fetch_one`, `execute`) | async | none | Async-only escape hatch |
| Transactions | `atomic=True` | same | The runtime owns it |
| Atomic blocks | `async with db.atomic():`, `@db.atomic` on `async def` | `with db.atomic():`, `@db.atomic` on `def` | Sync forms raise `RuntimeError` on the loop ([transactions](transactions.md#atomic-blocks-and-functions-in-application-code)) |
| `Schema`, `Request`, `Response`, parsers, renderers | plain code | same | No I/O |
| Password hashing | `await hasher.hash/verify` | `hash_sync`, `verify_sync` | Async calls use their own bounded pool |
| Error handlers (`app.error_handler`) | `async def` | `def` | Run on the loop: no blocking calls |
| Module lifespans | async context manager | none | Resources belong to the worker's loop |
| Startup and shutdown hooks | `async def hook(ctx)` | `def hook(ctx)` | `Hinoki(on_startup=..., on_shutdown=...)`; sync runs on the executor |
| Admin hooks (`stats`, `check`, `save`, `remove`) | `async def` | none | The admin runs on the loop |
| Tests | `ASGIClient` | `SyncClient` | Same behaviour |

## Size the pool

`execution.sync_workers` (default `8`) threads serve sync handlers and hooks; `execution.sync_queue` (default `64`) requests may wait for one. A full queue answers `503 overloaded` with `Retry-After`. See [settings](settings.md).

## Errors and edge cases

* **Sync hooks share the pool with sync handlers.** A flood of bad credentials or slow hooks can fill the queue and turn legitimate sync work into `503`. Keep sync hooks short, reject malformed credentials before touching the database, and prefer async hooks.
* **A sync auth hook runs before the action's transaction opens**, so it is never part of it.
* **Sync model calls work only inside the app's sync handlers and hooks.** In a test body, use the async API (an async fixture) or your own connection, also with `SyncClient`.
* **Async only:** module lifespans, admin hooks, background tasks (`ctx.tasks.start_soon` takes coroutines) and raw `Database` SQL. A sync error handler runs on the loop and must not call the ORM; its failure is treated as unhandled.
* **`SyncClient`** works only inside its `with` block and raises a timeout error after `timeout` seconds (default 60).

The test suite exercises sync and async handlers, sync authentication hooks, ORM calls through both clients, mixed traffic and cancellation against a disposable database.

## See also

* [Request lifecycle](request-lifecycle.md)
* [Transactions](transactions.md)
* [Settings](settings.md)
