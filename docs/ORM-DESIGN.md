# ORM design — spike and first slice (Q004 R7 / S2)

Status: **design** · 2026-11-13. Scope is deliberately small: a Rust database engine reachable from asyncio, transactions bound to the request, typed errors, and only then a minimal Active Record layer. Relations, migrations and CRUD generation are Q005.

## Observed

Runtime design ADR 0002 proposes a per-worker Tokio runtime bridged to asyncio. PostgreSQL is the first backend (spec §2). A disposable PostgreSQL 17 container is available for tests (`hinoki-test-pg`, 127.0.0.1:54399, tmpfs data, created for this work; the other Postgres on this machine belongs to another project and is never used).

## Recommendation

1. **Engine crate `crates/orm`** (pure Rust, no PyO3): `tokio-postgres` + `deadpool-postgres`. Driver choice: SQLx macros do not help with dynamically generated SQL (spec §3), and `tokio-postgres` exposes the protocol features we need (binary params, pipelining) with fewer moving parts. Revisit if a compile-time-checked static query layer is wanted.
2. **Bridge in `crates/bindings`**: `pyo3-async-runtimes` (tokio feature) turns Rust futures into awaitables on the *running* asyncio loop. One Tokio runtime per worker process, created on first use (never at import), `worker_threads` configurable (default 1). Cost is measured later (V10).
3. **Python surface `hinoki.db.Database`**: `await Database.connect(url, max_size=…)`, `await db.fetch(sql, *params) -> list[dict]`, `await db.execute(sql, *params) -> int`, `db.transaction()` async context manager, `await db.close()`. Plain SQL with `$1…` placeholders; parameters are typed Python values (None, bool, int, float, str, bytes, UUID, aware datetime, dict/list as JSON). No string interpolation anywhere in the framework.
4. **Transactions** hold one pooled connection from `BEGIN` to `COMMIT`/`ROLLBACK`; the active transaction lives in a `ContextVar`, so `db.fetch` inside `async with db.transaction()` uses it and a child task cannot silently share it concurrently (a second use while one statement is running raises). A transaction object dropped without finishing **discards its connection** instead of returning a dirty one to the pool.
5. **`transaction=True`** on an action: the adapter opens the transaction before the handler and commits only after the handler and output encoding succeed; any exception (including timeout/cancellation) rolls back *before* the error becomes an HTTP response. The `Database` is taken from the module context (`ctx.provide(Database, db)`); a route with `transaction=True` and no provided `Database` is a startup failure.
6. **Errors**: `DatabaseError` ⊃ `UniqueViolation`, `ForeignKeyViolation`, `NotNullViolation`, `CheckViolation`, mapped from SQLSTATE; they carry `constraint` and `table` but **never** SQL text, credentials or row values in `str(exc)`.
7. **Cancellation**: a cancelled asyncio task drops the Rust future; a statement in flight is abandoned and its connection is not reused unless the protocol state is known good (tokio-postgres drains it). Proven by a test (`pg_sleep` + timeout → rollback → pool still usable).
8. **Minimal Active Record (second step, same slice)**: `Model` with annotated fields, `aget/aget_or_none/acreate/aupdate/adelete`, `filter(...).order_by(...).aall()/apaginate()`. SQL generation for this subset lives in Rust (`crates/orm`: a small query AST → SQL + params), not in Python string building. Relations, M2M, migrations: Q005.

## Alternatives

* **asyncpg/psycopg from Python:** fastest route, but contradicts the Rust ORM requirement and removes the point of the native core.
* **SQLx:** excellent for static queries; weaker fit for AST-generated SQL; larger compile times.
* **Reusing Granian's Tokio runtime:** rejected (ADR 0002).

## Failure / empty / private states

Connect failure → `DatabaseError` at lifespan startup → startup failed (process exits, no traffic). Pool exhausted → bounded wait then `DatabaseError("pool timeout")` mapped to 503. Server messages are never returned to clients.

## Tests

Rust: AST→SQL goldens (no DB); engine integration against the disposable PostgreSQL when `HINOKI_TEST_DATABASE_URL` is set (skipped otherwise, and reported as skipped). Python: round-trip types, typed errors without leakage, transaction commit/rollback/discard, context isolation, timeout during `pg_sleep`, per-worker pools under Granian, `transaction=True` end to end.

## Steps and ownership

| Step | Deliverable | Files |
| --- | --- | --- |
| D1 | Engine: pool, typed params/rows, errors, transactions | `crates/orm/**` |
| D2 | PyO3 bridge + `hinoki.db.Database` | `crates/bindings/src/db.rs`, `python/hinoki/db.py`, `native.pyi` |
| D3 | `transaction=True` in the adapter, startup check | `python/hinoki/runtime/asgi.py`, `app.py` |
| D4 | Query AST → SQL in Rust, `Model` basics | `crates/orm/src/query.rs`, `python/hinoki/orm.py` |

## Open

NUMERIC/decimal and timestamp-without-zone mapping; prepared-statement cache sizing; SSL (TLS) configuration; PgBouncer compatibility; read replicas.
