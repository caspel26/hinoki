---
status: implemented
---

# Sync and async execution

Hinoki runs on one event loop per worker. **Async is the native way; sync is a supported way**, not a second-class one: a plain `def` handler or hook runs on a bounded thread pool (`settings.ExecutionSettings.sync_workers`, `sync_queue`; a full queue answers 503 `overloaded`, in handlers and in sync hooks), the request's context follows it, and the sync ORM methods hand their work back to the worker's loop and block that thread, never the loop.

## Naming convention

A name with the `a` prefix awaits the database (`aget`, `acreate`, `acount`); the same name without it is the blocking twin for sync handlers. A query builder is lazy until you evaluate it, and awaiting the queryset itself is the async twin of `.list()`:

```python
posts = await Post.filter(published=True).order_by("-created")  # async
posts = Post.filter(published=True).order_by("-created").list()  # sync handler
first = await Post.all().afirst()
```

`aall()` was removed: `await Post.all()` replaces `await Post.all().aall()`.

## Parity table

| Surface | Async | Sync | Notes |
| --- | --- | --- | --- |
| Action handlers | `async def` | `def` | Sync runs on the executor |
| Generated CRUD hooks (`get_queryset`, `written_queryset`, `to_schema`, `store`, `change`, `remove`) | `async def` | `def` | Sync overrides run on the same bounded executor; use the matching sync or async ORM methods |
| Auth providers (`authenticate`, `resolve`, `resolve_token`, JWT `resolve_claims`) | `async def` | `def` | Sync hooks run on the executor, so sync model calls are safe inside them |
| Model: create / get / update / delete | `acreate`, `aget`, `aget_or_none`, `aupdate`, `aupdate_where`, `adelete`, `asave`, `aupdate_from` | `create`, `get`, `get_or_none`, `update`, `update_where`, `delete`, `save`, `update_from` | Sync calls only work inside a sync handler or hook served by Hinoki. The sync writes take the same `returning=False` option as their async twins ([models](models.md#writes-that-do-not-read-the-row-back)) |
| QuerySet: read | `await queryset`, `afirst`, `aget`, `acount`, `aexists`, `apaginate` | `list`, `first`, `get`, `count`, `exists`, `paginate` | |
| QuerySet: write | `aupdate`, `adelete` | `update`, `delete` | |
| Relations | explicit loading (`select_related`, `prefetch_related`) | same | never lazy, so no hidden I/O in either style |
| Raw SQL (`Database.fetch`, `fetch_one`, `execute`) | async | none | Use the model/queryset methods from sync code; raw SQL is an async-only escape hatch |
| Transactions | `atomic=True` on the action | same | The runtime owns it for both styles |
| Atomic blocks in application code | `async with db.atomic():`, `@db.atomic` on an `async def` | `with db.atomic():`, `@db.atomic` on a plain `def` | The sync forms run on a worker thread served by Hinoki and raise `RuntimeError` on the event loop; `atomic=True` on the action covers a whole handler ([transactions](transactions.md#atomic-blocks-and-functions-in-application-code)) |
| `Schema`, `Request`, `Response`, parsers, renderers | plain code | same | No I/O |
| Password hashing | `await hasher.hash/verify` | `hash_sync`, `verify_sync` | The async calls use a bounded thread pool |
| Error handlers (`app.error_handler`) | `async def` | `def` | Run on the loop: no blocking calls inside |
| Module lifespans | async context manager | none | Resources belong to the worker's loop |
| App startup/shutdown hooks | `async def hook(ctx)` | `def hook(ctx)` on the sync executor | Listed in `Hinoki(on_startup=..., on_shutdown=...)` |
| Admin hooks (`stats`, `check`, `save`, `remove`) | `async def` | none | The admin runs on the loop; override with `async def` |
| Tests | `ASGIClient` | `SyncClient` | Same behaviour through both |
| Dev MCP `run_scenario` | n/a | n/a | Runs the app in-process |

## Limits to know

* **Sync hooks use the same pool as sync handlers.** Sync auth and CRUD hooks take an executor slot; a flood of bad credentials or long-running hooks can fill the queue and make legitimate sync work answer 503 (`overloaded`, with `Retry-After`). Keep sync hooks short, reject malformed credentials before touching the database, and prefer async hooks when they can use async APIs.
* **A sync auth hook runs before the action's transaction opens**, so it is never part of it.
* **Sync model calls only work inside the application's sync handlers and hooks.** In a test body use the async API (an async fixture) or your own connection, also with `SyncClient`.
* **No sync lifespan, sync admin hooks, sync background tasks (`ctx.tasks.start_soon` takes coroutines) or sync `Database` SQL.** A sync *error handler* runs on the loop and must not call the ORM (its failure is treated as "unhandled").
* **`SyncClient`** is available only inside its `with` block and raises a timeout error after `timeout` seconds.

## Rules of thumb

* Prefer `async` handlers; use `def` for code that is naturally blocking (a library without async support, CPU work measured to be short).
* Never call blocking code from an `async def`; never call an `a...` coroutine from a `def` without `run_sync`-style helpers (the sync model methods already do that for you).
* CPU-heavy work belongs in a separate process or worker, not in either pool.
* A sync call made on the event loop itself raises, with a message naming the `a...` variant.

## Verified

The test suite exercises sync and async handlers, synchronous authentication hooks, ORM calls through both clients, mixed traffic and cancellation against a disposable database. There are no synchronous module lifespans, admin hooks or background task APIs; those surfaces are async-only by design.
