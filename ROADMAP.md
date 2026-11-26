# Hinoki roadmap

Hinoki is pre-alpha. This is a short public view of the work; implementation status may change as the project develops.

For the planned `0.1.x` line, documented Python, CLI, settings and runtime
contracts, plus migration files, stay compatible across patch releases. The
[project status](docs/status.md) records the migration-format limits and the
pre-1.0 break policy.

## Available today

- Typed Python HTTP APIs with optional architecture rules and declared errors.
- Outbound email over SMTP (plus `console` and `memory` backends) from a per-worker mailer, with the message rendering, the SMTP conversation, TLS, the pool and retries in Rust. No queue, no delivery guarantee and no HTTP providers. Email templates are a small fixed language with typed contexts, rendered in Rust and checked when a worker starts. See [send email](docs/email.md) and [email templates](docs/email-templates.md).
- The Rust extension currently provides path matching, selected request parsing, schema validation/serialization, cryptography, and PostgreSQL, SQLite, MySQL and MariaDB ORM execution. The ASGI adapter and application handlers run in Python, except that, on Kiso (the default), an `async def` handler whose only awaits are ORM reads and stock writes the framework can prove safe (guarded, body and `atomic` routes included) is driven from Rust without an asyncio task; eligible public generated `create`, `update` and `delete` actions run their transaction in Rust. SQLite is the local-development default.
- Sync and async handlers, settings, modules, migrations, OpenAPI, MCP tools, model lifecycle hooks, `bulk_*`/`abulk_*` writes, atomic writes (`atomic=True` on actions, `db.atomic` as a block or decorator in application code), response checks against annotations, route rate limit policies, and a built-in admin with four themes, explicit mounting and its own users.
- Typed `Resource` injection into handlers with startup provider checks; bounded file storage, application sessions with CSRF and `SessionAuth`, and cache backends (worker-local Rust memory or shared database).
- Typed `http.Middleware` that runs identically on Kiso and Granian (it moves the matched routes onto the Python lane), and WSGI callables served by Kiso with `--interface wsgi` through a bounded thread pool and a streamed response. The WSGI path crosses Python objects and is not native.
- Streaming responses: `Stream`, `EventStream` (server-sent events) and `File` on Kiso and Granian, with a bounded buffer, an idle timeout, keep-alive comments and a disconnect check. The application's iterator is Python on both servers; Kiso frames and queues the pieces in Rust and sends a `File` without its bytes entering Python. See [stream a response](docs/streaming.md).
- Optional response compression on Kiso only (`compression.enabled`, off by default): brotli and gzip negotiated, applied and bounded in Rust, with only the settings in Python. See [compress responses](docs/compression.md).
- Reload on `SIGHUP` in Kiso (`hinoki serve`, POSIX): the supervisor, which is Python, starts new workers one at a time, waits until each serves, then stops the old one gracefully, all on the same listening socket. See [Kiso](docs/kiso.md#reload-on-sighup).
- WebSockets (`@http.websocket`) on Kiso, with framing and limits in Rust, and through the ASGI `websocket` scope on Granian. See [WebSockets](docs/websockets.md).
- Typed events with explicit handlers (`in_transaction` and `after_commit`), including model events and `AuthRejected`/`RateLimited`. See [events](docs/events.md).
- Composable model lifecycle utilities: `UUIDModel`, `TimestampedModel`, `SoftDeleteModel`, `uuid_pk()` (UUIDv4, Rust-generated RFC 9562 UUIDv7, or custom factory), `urlsafe_token()` (Rust CSPRNG), `@db.soft_delete()`, stackable `@db.track_status()`, soft-delete query scopes and explicit restore/hard-delete operations. See [models](docs/models.md#lifecycle-utilities).
- Generated CRUD over `SoftDeleteModel`, `UUIDModel` (UUIDv4/UUIDv7), `urlsafe_token()` and `Decimal`/`date` columns runs on Kiso's native CRUD lane; `Enum` columns and `CursorPagination` lists stay on the Python lane.
- Many-to-many relations (`many_to_many(...)`, generated CRUD ids fields, admin multi-select, MCP description); routes with an ids field run on the native generated-CRUD lane when targets are scoped with `all_rows(...)`/`own_rows(...)`, and stay on the Python lane with a custom scope, hook, soft-delete target or link event handler.
- Client filters, ordering and search on generated `crud(...)` lists through a `FilterSet`, parsed and compiled in Rust, with typed OpenAPI parameters; on Kiso, offset-paginated filtered lists without custom `get_queryset`/`filter_queryset` hooks run on the native generated-CRUD lane. See [CRUD](docs/crud.md#filter-order-and-search-the-list).
- OpenAPI 3.2 documents (3.1 with `docs.openapi_version`), with `QUERY` as the `query` operation (`x-query` in 3.1) and per-action `tags`, `deprecated`, `operation_id` and `include_in_schema`. See [OpenAPI](docs/openapi.md).
- A CORS policy for the whole application that an action, a `RouteGroup` or `crud(...)` can replace. See [per-route CORS](docs/web-security.md#cors-per-route).
- Prometheus metrics from a dedicated Kiso listener (off by default). See [metrics](docs/metrics.md).
- A [security policy](SECURITY.md) with private reporting, and scheduled fuzzing of Kiso's request-line, header, host, redirect, length and WebSocket handling.

See the [documentation](https://hinoki.caspel.dev/) and runnable [examples](examples/) for current details. No performance claims are made without comparable retained measurements.

## Active direction

- Improve the developer and documentation experience while keeping the public API small. `crud(...)` (function and class decorator, with `CrudAccess`) is the CRUD declaration, and its class form carries the `get_queryset`, `to_schema`, `written_queryset`, `store`, `change` and `remove` hooks.
- Maintain `Schema` within its core web-API scope across both the Python and Rust lanes, with OpenAPI, rules, differential tests and documentation (see [ADR 0006](docs/adr/0006-schema-scope-and-parity.md)). Available: `dict[str, X]`, `date`, `time`, `timedelta`, `Decimal`, `bytes`, `Enum`, `StrEnum`, `IntEnum`, `Literal`, `set`, `frozenset`, `tuple`, plain unions and `Any`; `multiple_of` on `int` and `Decimal`; IP addresses and networks (also as `Model` columns) and the `Email`, `Url` and `Slug` annotations; `Secret` and `Path`; `TypedDict` and dataclass fields; `Field(strip=True)`, `Field(lower=True)` and `Field(upper=True)` for `str` fields; `pattern` on `bytes`; `Field(description=...)`, `examples` and `deprecated` for the OpenAPI document; `Schema.replace()` for a validated copy; `date`, `time`, `timedelta`, `Decimal` and `Enum` as `Model` columns; `@computed_field`; generic schemas (`class Envelope[T](Schema)` used as `Envelope[User]`); `@before_validate` to reshape raw input before validation (see [schemas](docs/schema-validation.md#reshape-input-with-before_validate)); `Decimal` model columns as `numeric` on PostgreSQL and `decimal(65,30)` on MySQL/MariaDB, filterable and orderable there (equality only on SQLite). Strings and dict keys containing NUL are refused with a 422. Custom validators (`field_check`, `field_normalize`, `object_check`) already exist and run on the Python lane by design (see [validation](docs/schema-validation.md#built-in-and-custom-validation)).
- Validate the server integration, admin styles and generated typing contracts across real usage.
- Cursor (keyset) pagination is available as `CursorPagination` for generated lists and handwritten routes (see [pagination](docs/pagination.md#cursor-keyset-pagination)); the admin lists keep page numbers.
- Before the first release (`0.1.0`):
    - a final audit of the public Python API surface (`hinoki`, `hinoki.http`, `hinoki.db`, `hinoki.orm`, `hinoki.admin`) before locking the `0.1.x` compatibility promise;
    - a clean-wheel dry-run verifying packaging (quickstart, migrations, admin static assets and `hinoki serve` from a built wheel);
    - a full review of the admin templates and styles across themes, viewports and interactive flows;
    - end-to-end validation of Hinoki on a production-ready application project;
    - a review of the security findings, and a review of the site and release process before publishing.

## Planned for 0.2.0

These are directions, not commitments, and none of them is available today.

- An optional `Schema`-typed Redis module (`RedisModule`) owning a per-worker connection pool in its lifespan: backs `RedisCache`, `RedisSessionStore`, and `RedisRateLimit` through the `0.1.0` cache, session and rate-limit protocols without changing handler signatures, and exposes a `Schema`-typed client for structured Redis commands (such as `hget` and `hset`).

## Planned for a later release

These are directions, not commitments, and none of them is available today.

- Moving more of the request path into the Rust core, before new features.
- A durable email queue and HTTP email providers (outbound email is SMTP only today).
- Built-in one-time credentials, with the secrets hashed or encrypted at rest and attempts bounded: single-use tokens for links (password reset, email verification, invitations), one-time codes sent by email, TOTP (RFC 6238) with recovery codes, and two-factor sign-in for the admin. They depend on rate limit storage shared by workers and on key rotation.
- Filters across relations and on the admin lists, and cursor pagination in the admin.
- Tracing, an outbound HTTP client with timeouts and retries, and key rotation.
- Range filters and ordering for `Decimal` on SQLite.
- Distributing as a single package, `hinoki-api` (import and CLI stay `hinoki`), with the admin remaining an optional built-in app of that package, activated explicitly, rather than a separate distribution.

This roadmap is intentionally curated; detailed queue notes and owner decisions are maintained locally and are not part of the public repository.
