# Best practices

Practical defaults for building, securing, optimizing, testing and operating a Hinoki application in production. Each recommendation below matches behavior the framework implements and checks; see [Structure an application](https://hinoki.caspel.dev/dev/patterns.md) for the three-layer architecture and architectural rules behind them.

## 1. Project structure and modules

### Separate HTTP, operations and persistence

Keep route handlers thin and push business rules into plain async operations that take typed values (`Caller`, `Schema`, IDs) rather than `Request` objects. That lets HTTP actions, management commands, admin hooks, MCP tools and tests share the same logic without duplicating checks.

* Use [function-based APIs](https://hinoki.caspel.dev/dev/function-endpoints.md) grouped with `RouteGroup` by default so shared `access=`, `atomic=` and `errors=` declarations live in one place.
* Use [`API` classes](https://hinoki.caspel.dev/dev/class-endpoints.md) when you need `@crud(...)` resources, lifecycle hooks or shared inheritance.
* Declare routes in each app's `routes.py` and keep URL prefixes in `hinoki.toml` ([register routes](https://hinoki.caspel.dev/dev/routes.md)).
* Enforce layer direction with `Imports.forbid` in your project rules ([rules](https://hinoki.caspel.dev/dev/rules.md)).

### Own worker resources with `Module`, never at import time

Importing a Python module must not open sockets, pools or files, and must not hold global mutable per-request state. Wrap every long-lived client or pool in a `Module` whose `lifespan` opens the resource per worker, exposes it via `ctx.provide(...)`, and closes it in `finally`:

```python
# notes/passwords.py
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from hinoki import Module, ModuleContext, Resource
from hinoki.crypto import PasswordHasher


class Passwords(Module):
    name = "passwords"

    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        hasher = PasswordHasher()
        ctx.provide(PasswordHasher, hasher)
        try:
            yield
        finally:
            hasher.close()
```

Declare `requires = (...)` when one module depends on another so startup and shutdown order stay explicit. If handlers need a worker-owned dependency directly, subclass `Resource`, provide it from a `Module`, and annotate it on the handler signature ([application modules](https://hinoki.caspel.dev/dev/modules.md#inject-a-worker-resource-into-a-handler)). For the database, declaring `database: settings.DatabaseSettings` on your settings class automatically installs `DatabaseModule` per worker.

### Type configuration with `AppSettings` and `Secret`

Subclass `settings.AppSettings` once for the project, give it a prefix, and type every credential as `settings.Secret`:

```python
# notes/settings.py
from hinoki import settings


class ProjectSettings(
    settings.AppSettings, prefix="NOTES_", dotenv=".env", toml="settings.toml"
):
    database: settings.DatabaseSettings
    jwt_key: settings.Secret
```

* Never hardcode secret defaults or commit secrets in `settings.toml` (TOML rejects `Secret` fields at load).
* Supply secrets through environment variables (`NOTES_JWT_KEY`) or container secret files (`NOTES_JWT_KEY_FILE=/run/secrets/jwt_key`).
* Call `.reveal()` only at the boundary that constructs the client or key (`JWTKey("2026-11", "HS256", app_settings.jwt_key)` accepts `Secret` directly without revealing it). `Secret` redacts itself in `str`, `repr`, logs and `hinoki settings`.
* Keep `strict=True` (the default) so typos in prefixed environment variables fail at startup instead of silently falling back to defaults. See [settings](https://hinoki.caspel.dev/dev/settings.md).

## 2. API contracts and `Schema`

### Separate input, patch and output `Schema` classes

Models describe tables; `Schema` classes describe the public wire contract. Never return a `Model` instance directly from an action. Define separate `Schema` classes by role:

```python
# notes/apps/billing/schemas.py
from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from hinoki import UNSET, Field, Schema, Unset, derive

from .models import Invoice


class InvoiceIn(Schema):
    reference: Annotated[str, Field(min_length=1, max_length=64)]
    amount: Annotated[Decimal, Field(gt=0, max_digits=10, decimal_places=2)]
    due_at: datetime


class InvoicePatch(Schema):
    reference: Annotated[str, Field(min_length=1, max_length=64)] | Unset = UNSET
    paid: bool | Unset = UNSET


@derive(Invoice, exclude=("internal_notes",))
class InvoiceOut(Schema):
    pass
```

* Omit server-assigned fields (`id`, `author_id`, `tenant_id`, `created_at`) from input `Schema` classes, and pass them explicitly as keyword arguments to `Model.acreate(data, author_id=caller.user.id)` so server values always win.
* Use `UNSET` defaults on patch `Schema` classes and apply them with `await row.aupdate_from(patch)` so only sent fields are written.
* Turn on `Actions.forbid_model_responses()` and `Schemas.forbid_exposure("password_hash", ...)` in your [rules](https://hinoki.caspel.dev/dev/rules.md) to prevent accidental leaks. See [schemas](https://hinoki.caspel.dev/dev/schemas.md) and [schemas from models](https://hinoki.caspel.dev/dev/model-schemas.md).

### Keep `validate_response=True` enabled

`@http.*` and `RouteGroup` enable `validate_response=True` by default. Leave it enabled: Hinoki checks the handler's return value against its return annotation before encoding the response, catching contract drift early and rolling back an `atomic=True` transaction instead of committing after a broken answer. Reach for `validate_response=False` only for intentional escape hatches where the return type cannot be checked ([requests and responses](https://hinoki.caspel.dev/dev/endpoints.md#the-response-is-checked-against-the-annotation)).

### Use strict, explicit scalar types and avoid lazy I/O

* Use timezone-aware `datetime` (and `time` with an offset on the wire), `UUID` for opaque identifiers, `Decimal` with `max_digits`/`decimal_places` for money (never `float`), and `Enum` or `Literal` for closed sets ([scalar field types](https://hinoki.caspel.dev/dev/schema-types.md)).
* Never trigger I/O during `Schema` serialization. Relations in Hinoki are never lazy, so load every related row before calling `from_model(...)` or projecting.

## 3. Security, authentication and `Access`

### Centralize authorization in reusable `Access` policies

Endpoints are public by default. Define authentication providers (`BearerAuth`, `JWTAuth`, `ClaimsJWTAuth`, `APIKeyAuth`, `HeaderAuth`) and `Permission` enums once, compose them into named `Access` policies, and attach them on the `RouteGroup` or action:

```python
# notes/security.py
from hinoki import Access, BearerAuth, Caller, Permission


class BillingPermission(Permission):
    READ = "billing:read"
    WRITE = "billing:write"


class TokenAuth(BearerAuth[User]):
    async def resolve_token(self, token: str) -> Caller[User] | None: ...


auth = TokenAuth()
billing_reader = Access(auth=auth, permissions=[BillingPermission.READ])
billing_writer = Access(auth=auth, permissions=[BillingPermission.WRITE])
```

Keep `Actions.require_access` (`HNK-ACC-001`) and `HNK-ACC-002` enabled so every public or permissionless action is an explicit decision ([authentication and access](https://hinoki.caspel.dev/dev/authentication.md), [rules](https://hinoki.caspel.dev/dev/rules.md)).

### Enforce ownership and tenant scoping everywhere

Permissions control *what* a caller may do; your queries control *which rows* they may touch. Enforce the same scope across manual actions, generated CRUD, MCP tools and admin:

* **Manual writes:** fold ownership or tenant checks into the write statement itself (`await Invoice.aupdate_where({"id": invoice_id, "tenant_id": caller.user.tenant_id}, {"paid": True})`) to avoid check-then-write races.
* **Generated CRUD:** configure `owner=` / `own_rows(...)` or override `get_queryset(self, principal)` on the `API` class ([generated CRUD](https://hinoki.caspel.dev/dev/crud.md)).
* **Admin and MCP:** override `ModelAdmin.queryset(self, principal)` and `ModelAdmin.can(...)` in the [admin](https://hinoki.caspel.dev/dev/admin.md); [MCP tools](https://hinoki.caspel.dev/dev/app-mcp.md) reuse the action's `Access` policy and handler directly.

### Apply rate limits, CORS, host checks and safe errors

* **Rate limiting:** attach `RateLimit(limit, window, ...)` to sensitive actions such as login or token issuance (`@http.post("/auth/login", rate_limit=RateLimit(5, 60, headers=True), atomic=True)`). The default `DatabaseLimits()` storage shares counters across every worker and host via the `hinoki_rate_limits` table (apply its migration at deploy time); use `MemoryLimits()` only in development or tests. Keep `on_error="closed"` (the default) on security-critical endpoints so a storage outage fails closed with `503` ([rate limiting](https://hinoki.caspel.dev/dev/endpoints.md#rate-limit-an-action)).
* **Hosts, CORS and HTTPS:** before deploying, narrow `allowed_hosts` from `*` to the exact host names you serve, list explicit browser origins in `cors.origins` (or per-route `http.Cors(...)`, never `*` with `credentials=True`), and enable `security.redirect_https` and `security.hsts_seconds` alongside exact `server.trusted_proxies` CIDRs when behind a reverse proxy ([hosts, CORS and HTTPS](https://hinoki.caspel.dev/dev/web-security.md)).
* **Safe errors:** keep `debug = false` in production so unhandled exceptions return `500 internal_error` with a `request_id` and no traceback or exception message. Raise declared `APIError` subclasses (`errors=(InvoiceAlreadyPaid,)`) and do not catch broad `Exception` inside handlers — let the framework roll back and translate the error ([errors](https://hinoki.caspel.dev/dev/errors.md)).

## 4. ORM, queries and transactions

### Eliminate N+1 queries and use bulk writes

Hinoki never lazy-loads relations; accessing an unloaded relation raises `RelationNotLoaded`. Load exactly what the response needs in the query that fetches the rows:

* Use `select_related("customer")` for forward foreign keys (one `LEFT JOIN`).
* Use `prefetch_related("invoices")` (or scoped `prefetch_related(tags=...)`) for reverse foreign keys and many-to-many relations (bounded chunked queries). See [relations](https://hinoki.caspel.dev/dev/relations.md).

For multi-row writes, prefer bulk methods over loops:

```python
# Batch writes instead of awaiting inside a loop
created = await Invoice.abulk_create(items, returning=False)
updated = await Invoice.abulk_update(modified_rows)
deleted = await Invoice.abulk_delete(obsolete_ids)
count = await Invoice.filter(paid=False, overdue=True).aupdate(
    {"reminded": True}, returning=False
)
```

Pass `returning=False` on `abulk_create` and `QuerySet.aupdate` when you do not need the written rows back ([query and write rows](https://hinoki.caspel.dev/dev/queries.md)).

### Scope transactions tightly and respect service boundaries

* Put `atomic=True` on multi-step write actions (or on the `RouteGroup`, setting `atomic=False` on pure reads and no-write `POST` actions). `atomic=True` commits only after the response is validated and encoded, and rolls back before error translation.
* Never return `Response(status=4xx)` to signal an error from an `atomic=True` action: a returned `Response` commits the transaction. Raise an `APIError` instead.
* Use `async with db.atomic():` or `@db.atomic` inside reusable operations; nested scopes automatically become savepoints.
* Never perform irreversible external side effects (sending email, publishing messages) inside an open transaction. Register them with `current_database().after_commit(...)` so they run only after the outermost commit ([transactions](https://hinoki.caspel.dev/dev/transactions.md), [send email](https://hinoki.caspel.dev/dev/email.md)).
* Never join across service databases or pretend a local transaction spans a remote HTTP call. Store foreign service IDs as plain values, wrap remote calls in operations with explicit timeouts, and design multi-service writes to be idempotent ([more than one service](https://hinoki.caspel.dev/dev/patterns.md#12-more-than-one-service)).

### Paginate every list and choose the right strategy

Never return an unbounded table scan; enforce `Actions.require_pagination` in your project rules.

* **`PageNumberPagination()`** when clients need page numbers and a `total` count. On read-only endpoints (`atomic=False`) backed by PostgreSQL or MySQL with `database.max_size >= 2`, pass `concurrent=True` to run the `COUNT(*)` and page-row queries in parallel (inside a transaction or on SQLite, Hinoki automatically runs them sequentially).
* **`CursorPagination()`** for deep or rapidly growing feeds where constant-time pages matter and `total` is unnecessary. Set a 32+ byte `pagination.cursor_secret` via the environment or `*_FILE`, index the sort columns in sort order, and never order by a sensitive column (cursors are HMAC-signed, not encrypted). See [pagination](https://hinoki.caspel.dev/dev/pagination.md).

## 5. Performance and Rust fast paths

### Project read-only lists and pages directly in Rust

When a read-only endpoint returns a list or page of a `Schema` without mutating the items in Python, use `aproject(Schema)` or `apaginate_as(Schema, page, concurrent=True)`:

```python
# notes/apps/billing/api.py
from hinoki import RouteGroup
from hinoki.pagination import Page, PageNumberPagination, PageParams

from .models import Invoice
from .schemas import InvoiceOut

invoices = RouteGroup("/invoices", access=billing_reader)


@invoices.get("/", pagination=PageNumberPagination())
async def list_invoices(page: PageParams) -> Page[InvoiceOut]:
    return (
        await Invoice.all()
        .select_related("customer")
        .apaginate_as(InvoiceOut, page, concurrent=True)
    )
```

`aproject` and `apaginate_as` (and their sync twins `project` and `paginate_as`) encode eligible scalar and `select_related` schemas directly from Rust rows into JSON bytes without constructing intermediate Python `Model` or `Schema` objects. Accessing `.items` or iterating materializes Python `Schema` instances lazily from the owned rows without a second query, and schemas with computed fields, hooks, validators or `prefetch_related` fall back to normal hydration transparently ([pagination](https://hinoki.caspel.dev/dev/pagination.md#direct-schema-projection), [queries](https://hinoki.caspel.dev/dev/queries.md)).

### Keep hot routes on Kiso's native lane

On [Kiso](https://hinoki.caspel.dev/dev/kiso.md), eligible routes skip Python routing and request binding, and plain `async def` handlers whose `await`s are stock ORM calls can run on the native handler lane without an asyncio task.

* Use built-in parsers and renderers (`JSONParser`, `JSONRenderer`, etc.) and plain `async def` handlers without wrapper decorators when you want native dispatch.
* For stateless bearer tokens, `ClaimsJWTAuth` verifies tokens and answers `401`/`403` refusals in Rust without entering Python ([claims-only tokens](https://hinoki.caspel.dev/dev/authentication.md#claims-only-tokens-on-the-native-lane)).
* Inspect every route's lane and why any route falls back to Python with:

```sh
uv run hinoki routes --lanes
```

See [inspect an application](https://hinoki.caspel.dev/dev/inspect.md#routes) and [Kiso server](https://hinoki.caspel.dev/dev/kiso.md#the-native-handler-lane).

### Never block the event loop; size workers and pools together

* Write `async def` handlers with `a...` ORM methods by default. Never call blocking network, file or database libraries inside an `async def` handler.
* If you must call a blocking library, make the handler a plain `def` and use the sync ORM twins (`get`, `create`, `paginate`, `with db.atomic():`) so Hinoki runs it on the bounded sync executor (`execution.sync_workers`, default `8`; `execution.sync_queue`, default `64`). Beyond both, requests answer `503 overloaded` ([sync and async code](https://hinoki.caspel.dev/dev/sync-async.md)).
* Size `server.workers`, `server.runtime_threads` and `database.max_size` together. Each worker owns its own database pool, and a rolling `SIGHUP` reload on Kiso runs at most one extra worker at a time, so budget:

```text
replicas × (server.workers + 1) × database.max_size <= database max connections
```

Keep `server.runtime_threads` (default `1`) small unless profiling shows the Rust runtime threads are saturated, and measure changes against a baseline instead of guessing ([settings](https://hinoki.caspel.dev/dev/settings.md#8-database-connection-ownership)).

## 6. Testing

### Use disposable databases, synthetic fixtures and `ASGIClient`

Tests must never touch production data, real accounts or external mail servers. Point test settings at a throwaway database (an isolated per-test SQLite file migrated with `migrator.apply`, or disposable PostgreSQL/MySQL/MariaDB servers via `HINOKI_TEST_DATABASE_URL`, `HINOKI_TEST_MYSQL_URL` and `HINOKI_TEST_MARIADB_URL`) and drive requests in-process through `ASGIClient` (or `SyncClient` for synchronous tests) from `hinoki.testing`:

```python
# tests/billingtest.py
from hinoki.testing import ASGIClient

from notes.main import create_app


async def test_invoice_access_and_validation(test_settings) -> None:
    app = create_app(test_settings)
    async with ASGIClient(app, strict_errors=True) as client:
        # Anonymous and unauthorized callers are refused
        assert (await client.get("/billing/invoices")).status == 401
        assert (await client.get("/billing/invoices", token=viewer_token)).status == 403

        # Invalid payloads fail with 422 before hitting the handler
        bad = await client.post(
            "/billing/invoices",
            token=staff_token,
            json={"reference": "", "amount": "-5.00"},
        )
        assert bad.status == 422
```

* Pass `strict_errors=True` to `ASGIClient` so any error response missing from an action's `errors=` declaration fails the test before OpenAPI can drift.
* Test every `Access` policy with denial cases (`401` missing/invalid credential, `403` missing permission, and cross-tenant or non-owner access) as well as happy paths.
* Inject `ManualClock` from `hinoki.testing` for deterministic timestamps, token expiry and rate-limit windows, and use `EmailBackend.MEMORY` to assert on `mailer.outbox` without network I/O ([test an application](https://hinoki.caspel.dev/dev/testing.md)).

## 7. Production and operations

### Run migrations as a dedicated deploy step

Never run migrations from a worker lifespan or startup hook. Plan and review SQL migrations during development, then apply them once per deployment from a single release job before rolling the new application workers:

```sh
uv run hinoki migrations plan billing
uv run hinoki migrations apply
```

Check for pending or destructive changes in CI before deploying ([migrations](https://hinoki.caspel.dev/dev/migrations.md)).

### Serve on Kiso with graceful shutdown and startup probes

Run `hinoki serve` (which uses Kiso by default) behind your process manager or container orchestrator with `debug = false`, explicit `allowed_hosts`, and either TLS (`server.tls_cert_file`, `server.tls_key_file`, optional `server.http2`) or an ingress listed in `server.trusted_proxies`:

```toml
# settings.toml
allowed_hosts = ["api.example.com"]

[server]
workers = 4
sighup = "reload"
access_log_format = "json"
access_log_skip = ["/health"]
access_log_slow_ms = 200

[lifecycle]
startup_timeout = 30.0
shutdown_timeout = 15.0

[logging]
level = "info"
format = "json"
include_request_id = true

[metrics]
listen = "127.0.0.1:9100"
```

* **Graceful shutdown and reloads:** `SIGTERM` stops accepting new connections, drains in-flight requests for up to `lifecycle.shutdown_timeout`, and runs module shutdown cleanly. Sending `SIGHUP` to the Kiso supervisor (when `server.sighup = "reload"`, the default) replaces workers one by one without closing the listening socket ([Kiso server](https://hinoki.caspel.dev/dev/kiso.md#reload-on-sighup)).
* **Startup probe:** opt in with `Hinoki(startup_url="/health")` to expose a fixed `GET`/`HEAD` endpoint returning `200 {"status":"ok"}` once worker startup completes (`503 not_ready` before startup or during shutdown). Treat it as a startup probe — it does not query the database or downstream services ([optional startup probe](https://hinoki.caspel.dev/dev/serving.md#optional-startup-probe)).
* **Metrics and structured logs:** enable Kiso's dedicated Prometheus listener via `metrics.listen` (protected by `metrics.allow` or `metrics.token` whenever bound off loopback; never exposed on the application port), emit JSON application logs with `logging.format = "json"` and `logging.include_request_id = true`, and emit JSON access logs with `server.access_log_format = "json"` ([export Prometheus metrics](https://hinoki.caspel.dev/dev/metrics.md), [serve an application](https://hinoki.caspel.dev/dev/serving.md)).

### Pre-deploy checklist

Before shipping a release:

1. Run `uv run hinoki check` and `uv run pytest` with zero failures.
2. Run `uv run hinoki routes --lanes` to confirm critical routes take the expected serving lane.
3. Confirm `debug` is off, `allowed_hosts` is narrowed, and all `Secret` values come from the environment or `*_FILE`.
4. Apply migrations once (`uv run hinoki migrations apply`) before starting or reloading workers.
5. Verify `GET /health` (if enabled) and the Prometheus `/metrics` scrape after rollout.
