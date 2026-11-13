# Sync and async: what works where (Q018)

Hinoki runs on one event loop per worker. **Async is the native way; sync is a supported way**, not a second-class one: a plain `def` handler or hook runs on a bounded thread pool (`ExecutionSettings.sync_workers`, `sync_queue`; a full queue answers 503 `overloaded`, in handlers and in sync hooks), the request's context follows it, and the sync ORM methods hand their work back to the worker's loop and block that thread, never the loop.

## Parity table

| Surface | Async | Sync | Notes |
| --- | --- | --- | --- |
| Action handlers | `async def` | `def` | Sync runs on the executor |
| Auth providers (`authenticate`, `resolve`, `resolve_token`, JWT `resolve_claims`) | `async def` | `def` | Sync hooks run on the executor, so sync model calls are safe inside them |
| Model: create / get / update / delete | `acreate`, `aget`, `aget_or_none`, `aupdate`, `aupdate_where`, `adelete`, `asave`, `aupdate_from` | `create`, `get`, `get_or_none`, `update`, `update_where`, `delete`, `save`, `update_from` | Sync calls only work inside a sync handler or hook served by Hinoki |
| QuerySet: read | `aall`, `afirst`, `aget`, `acount`, `aexists`, `apaginate` | `list`, `first`, `get`, `count`, `exists`, `paginate` | |
| QuerySet: write | `aupdate`, `adelete` | `update`, `delete` | |
| Relations | explicit loading (`select_related`, `prefetch_related`) | same | never lazy, so no hidden I/O in either style |
| Raw SQL (`Database.fetch`, `fetch_one`, `execute`) | async | none | Use the model/queryset methods from sync code; raw SQL is an async-only escape hatch |
| Transactions | `transaction=True` on the action | same | The runtime owns it for both styles |
| `Schema`, `Request`, `Reply`, parsers, renderers | plain code | same | No I/O |
| Password hashing | `await hasher.hash/verify` | `hash_sync`, `verify_sync` | The async calls use a bounded thread pool |
| Error handlers (`app.errors.handle`) | `async def` | `def` | Run on the loop: no blocking calls inside |
| Module lifespans | async context manager | none | Resources belong to the worker's loop |
| Admin hooks (`stats`, `check`, `save`, `remove`) | `async def` | none | The admin runs on the loop; override with `async def` |
| Tests | `ASGIClient` | `SyncClient` | Same behaviour through both |
| Dev MCP `run_scenario` | n/a | n/a | Runs the app in-process |

## Limits to know

* **Sync hooks use the same pool as sync handlers.** A request to a protected route with a plain-`def` auth hook takes an executor slot for the whole hook, also when its credentials are wrong; a flood of bad tokens can fill the queue and make legitimate sync work answer 503 (`overloaded`, with `Retry-After`). Keep sync hooks short, reject malformed credentials before touching the database, and prefer an `async def` provider on exposed endpoints.
* **A sync auth hook runs before the action's transaction opens**, so it is never part of it.
* **Sync model calls only work inside the application's sync handlers and hooks.** In a test body use the async API (an async fixture) or your own connection, also with `SyncClient`.
* **No sync lifespan, sync admin hooks, sync background tasks (`ctx.tasks.start_soon` takes coroutines) or sync `Database` SQL.** A sync *error handler* runs on the loop and must not call the ORM (its failure is treated as "unhandled").
* **`SyncClient`** refuses use outside its `with` block and gives up after `timeout` seconds instead of hanging.

## Rules of thumb

* Prefer `async` handlers; use `def` for code that is naturally blocking (a library without async support, CPU work measured to be short).
* Never call blocking code from an `async def`; never call an `a...` coroutine from a `def` without `run_sync`-style helpers (the sync model methods already do that for you).
* CPU-heavy work belongs in a separate process or worker, not in either pool.
* A sync call made on the event loop itself raises, with a message naming the `a...` variant.

## Verified

`tests/syncasynctest.py` runs the same scenarios (sync and async handlers, a sync auth provider using sync model calls, sync `update_where`/queryset `update`) through both clients against a real PostgreSQL; `tests/syncmodelstest.py` covers the thread/loop hand-off and cancellation; `tests/authtest.py` covers a plain-`def` provider. Not covered yet: sustained load with mixed sync and async traffic, and a sync lifespan or sync admin hooks (by design, see the table).
