# 📋 Release Notes

All notable user-facing changes to Hinoki are recorded here, newest first. Hinoki is pre-alpha, so any release may change the API and the migration format. Entries below `Unreleased` have not been published; this repository has no tagged release yet.

## [Unreleased]

---

### ✨ New Features

#### ⚡ Concurrent pagination and guarded Rust row-to-JSON projection
> `crates/orm/src/database.rs`, `crates/bindings/src/db.rs`, `crates/bindings/src/rows.rs`, `crates/bindings/src/request/json.rs`, `python/hinoki/orm/projection.py`, `python/hinoki/orm/queryset.py`, `docs/pagination.md`, `docs/queries.md`

`QuerySet.apaginate(..., concurrent=True)` runs the page rows query and the `COUNT(*)` query concurrently on connection pools with at least two connections outside transactions, falling back to sequential execution inside transactions and on SQLite. `QuerySet.aproject` / `project` and `QuerySet.apaginate_as` / `paginate_as` project rows directly to UTF-8 JSON bytes in Rust off-GIL with lazy Python materialization on element/item access, preserving exact JSON parity and `validate_response=True` checks.

#### 🤖 `hinoki llms` and an opt-in `llms_url`
> `python/hinoki/ai/llms.py`, `python/hinoki/cli/llms.py`, `python/hinoki/app.py`, `python/hinoki/settings.py`, `python/hinoki/docsui/__init__.py`, `python/hinoki/runtime/pipeline.py`, `docs/openapi.md`, `docs/inspect.md`

`hinoki llms [TARGET] [--output FILE | --check FILE]` renders the application's OpenAPI document as an [llms.txt](https://llmstxt.org/) Markdown text for a coding agent: operations grouped by first tag with summary, deprecation, security scheme names and request and response components, `QUERY` included in both OpenAPI versions. Like `hinoki openapi` it opens no database, and `--check` exits `1` on drift. `Hinoki(llms_url="/llms.txt")` or the `docs.llms_url` setting serves the same text as `text/markdown`, off by default and without needing `openapi_url`; it answers `HEAD` like the OpenAPI document and leaves out what the document leaves out. See [describe the API for agents](docs/openapi.md#llms).

#### 🧭 `AGENTS.md` points at Hinoki's docs for agents
> `python/hinoki/scaffold.py`, `docs/ai-workflow.md`

The `AGENTS.md` that `hinoki new` writes names `https://hinoki.caspel.dev/llms.txt`, `llms-full.txt` and the per-page Markdown, and tells the agent to prefer them and the development MCP over guessing from other frameworks.

#### 🔌 WebSockets on `API` classes and typed decorators
> `python/hinoki/websocket.py`, `python/hinoki/api.py`, `python/hinoki/app.py`, `python/hinoki/runtime/asgi.py`, `docs/websockets.md`

`@http.websocket` works on `API` class methods under the class prefix, keeps the handler signature for type checkers and reads annotations at include time, so forward references resolve. Under ASGI a refusal waits for `websocket.connect`. Non-finite `float` parameters are refused.

#### 📎 `Form[Schema]` next to uploaded files
> `python/hinoki/runtime/binding.py`, `python/hinoki/runtime/requests.py`, `python/hinoki/openapi.py`, `docs/endpoints.md`

A `Form[Schema]` parameter binds the text parts of a multipart body next to `UploadedFile` parameters, and OpenAPI documents the body as one object with the schema's fields and the files.

#### 🧩 Model lifecycle utilities
> `crates/bindings/src/crypto.rs`, `python/hinoki/utils.py`, `python/hinoki/orm/model.py`, `python/hinoki/orm/mixins.py`, `python/hinoki/orm/options.py`, `python/hinoki/orm/queryset.py`, `python/hinoki/admin/`, `docs/models.md`, `docs/queries.md`

Models can compose `UUIDModel`, `TimestampedModel` and `SoftDeleteModel`, declare configurable UUID primary keys with `uuid_pk()` (UUIDv4 by default, Rust-generated RFC 9562 `version=7` / `uuid7()`, or any custom `default_factory`), declare Rust CSPRNG URL-safe token columns with `urlsafe_token()`, mark a nullable datetime with `@db.soft_delete()`, and stack `@db.track_status()` decorators to record status changes. Soft-deleted rows leave default model and reverse-relation queries; `with_deleted()` and `only_deleted()` provide explicit scopes, while restore and hard-delete APIs make physical deletion opt-in in the admin. Instance snapshots expose `previous()` and `changed_fields`. Ordinary unique constraints still include deleted rows; `ON DELETE` actions run only for hard deletes. Bulk `QuerySet.aupdate()` timestamps any included tracked field because it does not preload old values. See [model lifecycle utilities](docs/models.md#lifecycle-utilities), [queries](docs/queries.md) and [admin customization](docs/admin-customize.md#soft-deleted-models).

#### 🧩 Typed handler resources, storage, sessions and cache
> `python/hinoki/modules.py`, `python/hinoki/runtime/binding.py`, `python/hinoki/storage.py`, `python/hinoki/sessions.py`, `python/hinoki/cache.py`, `docs/modules.md`, `docs/storage.md`, `docs/sessions.md`, `docs/cache.md`

Handlers can declare concrete `Resource` parameters provided by application `Module`s; a missing provider is reported during startup. File storage offers bounded in-memory and local-disk backends, sessions offer memory and database stores with `SessionAuth`, and cache offers worker-local memory and shared-database backends. Each feature documents its resource ownership, cross-worker behavior, limits and errors; in-memory state remains local to its owning worker. See [modules](docs/modules.md), [file storage](docs/storage.md), [sessions](docs/sessions.md) and [cache](docs/cache.md).

#### 🧭 Typed `Caller` hooks and `from_user` in `@crud`
> `python/hinoki/resources.py`, `python/hinoki/runtime/crudplan.py`, `python/hinoki/__init__.py`, `examples/tracker/tracker/apps/tasks/api.py`, `examples/blog/blog/apps/blog/api.py`, `docs/crud.md`

`@crud` hooks (`get_queryset`, `filter_queryset`, `written_queryset`, `store`, `change`, `remove`) now accept a non-optional `Caller[User]` annotation when every generated action calling that hook is protected by an `Access` policy; declaring a non-optional `Caller` on a hook reachable by a public action, or mismatching `Caller[User]` against a parameterized `BaseAuth[User]`, fails at registration time with `RegistrationError`. `from_user("attr")` (also exported as `hinoki.from_user`) provides a declarative attribute extractor for `crud(..., assign=...)` as an alternative to `operator.attrgetter` while preserving full compatibility with arbitrary callables and the conservative Python-lane fallback. See [CRUD](docs/crud.md).

#### 🚦 Shared rate limit
> `python/hinoki/ratelimit.py`, `python/hinoki/ratelimit_store`, `python/hinoki/admin/users/limits.py`, `python/hinoki/runtime/pipeline.py`, `python/hinoki/runtime/kiso.py`, `python/hinoki/openapi.py`, `crates/ratelimit`, `crates/bindings/src/ratelimit.rs`, `crates/kiso/src/metrics.rs`, `crates/orm`

`hinoki.RateLimit(limit, window)` limits a route with counters in a database table shared by every worker and host (or in memory with `MemoryLimits`, for development). It is counted in the Rust core by a cell rate algorithm, in one statement per request, on async and sync handlers, and a route stays on Kiso's native handler lane. Options: `key` (`"client"`, `"caller"` or a function), `scope`, `tenant`, `cost`, `unknown_factor`, `on_error` (`"closed"` answers `503`, `"open"` admits), `headers=True` for `RateLimit-*` response headers (listed in OpenAPI), and `clock`. Keys are hashed before they reach SQL. The default scope is the method and the route template. The table `hinoki_rate_limits` (indexed on `state`) belongs to the optional `ratelimit` app (`hinoki app add ratelimit --models hinoki.ratelimit_store.models --migrations hinoki.ratelimit_store.migrations`); nothing creates it implicitly. `hinoki_rate_limit_decisions_total{decision}` counts decisions in the metrics. `Database::execute_scalar` (Rust ORM crate) runs a statement and returns its one value. Arguments are validated at construction (`timeout` 0.001 to 5 seconds, no bool, the integer maxima of the core). `MemoryLimits` keeps its buckets on the instance, so routes sharing it share counters. A worker's metrics replace an earlier worker's in the same process, and the limiters let go of the worker's database at shutdown.

Migration note: `DatabaseLoginWindow` of the admin now uses the same algorithm and a new table `hinoki_admin_rate_limits` (admin migration `0003-rate-limits`, apply it before using the policy). It no longer reads `hinoki_admin_login_windows`, which stays in place unused. `AdminAccounts` still defaults to the per-worker `LoginWindow`.

#### 📨 Email templates
> `python/hinoki/mail.py`, `python/hinoki/mailcli.py`, `python/hinoki/app.py`, `python/hinoki/rules.py`, `crates/mail/src/template`, `crates/bindings/src/mailtemplate.rs`, `crates/kiso/src/metrics.rs`, `examples/mailroom`

`mail.Template` renders a subject, an HTML body and an optional text body from a small fixed language (`{{ path }}`, `{% if %}`, `{% for %}`; no expressions, filters, includes or inheritance). Templates come from a TOML file (`Template.file`), TOML text (`Template.from_toml`) or strings, with a context `Schema` or a plain `dict`, and are listed in `Hinoki(email_templates=[...])` or `EmailModule(templates=[...])`. Parsing, checking, rendering, HTML escaping (inserted values only), the refusal of control characters in a subject value and the plain text derived from the HTML are Rust, with fixed limits. A template with a `Schema` is read and checked when a worker starts: an unknown variable, an unused field, a `Secret` field or a wrong shape stops the start. `template.message(context, to=...)` returns a `Message` for `mail.send`. Failures are `TemplateError` (kind `template`, with a `reason`, part, line, column and variable, never a value), counted as outcome `template` in `hinoki_email_messages_total` when a mailer is current. `hinoki mail check` and `hinoki mail preview NAME [--context FILE] [--part ...]` check and render templates without sending, and the opt-in rule `HNK-MAIL-001` (`hinoki.rules.EmailTemplatesMatch`) checks them in `hinoki check --allow-effects`. See [Email templates](docs/email-templates.md) and the `examples/mailroom` project.

#### ✉️ Send email
> `python/hinoki/mail.py`, `python/hinoki/settings.py`, `python/hinoki/app.py`, `python/hinoki/runtime/kiso.py`, `crates/mail`, `crates/bindings/src/mail.rs`, `crates/kiso/src/metrics.rs`

An `email: settings.EmailSettings` field installs `EmailModule`, which opens one `hinoki.mail.Mailer` per worker and closes it at shutdown. `await mail.send(mail.Message(to=("ada@example.com",), subject=..., text=..., html=...))` delivers over SMTP (STARTTLS, implicit TLS or, on the loopback or with `allow_insecure`, none; `PLAIN` and `LOGIN` authentication; a bounded pool; timeouts; retries of transient failures), or prints (`console`) or keeps (`memory`, `mailer.outbox`) the message. Rendering, header and address validation (a line break or other control character is refused; text that looks like an encoded word is encoded), limits, the SMTP conversation, TLS verification (it cannot be turned off) and the pool are Rust; the message and the result cross the binding as Python objects. Failures are `EmailError` with a `kind`, reply code, `attempts` and `possibly_sent`, and their text never holds an address, subject, body, password or server reply. Send after a commit with `current_database().after_commit(...)`; from a sync handler use `run_sync(mail.send(...))`. With `metrics.listen` on Kiso the families `hinoki_email_messages_total`, `hinoki_email_send_duration_seconds` and `hinoki_email_retries_total` are exported. There is no queue and no guaranteed delivery, no SMTPUTF8, XOAUTH2 or DKIM, and the admin sends no email. See [Send email](docs/email.md).

#### 🔗 Many-to-many relations
> `python/hinoki/orm/links.py`, `python/hinoki/resources.py`, `python/hinoki/schema/__init__.py`, `crates/orm/src/links.rs`, `crates/bindings/src/links.rs`

`tags: ClassVar[ManyToMany[Tag]] = many_to_many(Tag)` generates and migrates a bridge table and gives the owner `aadd`, `aremove`, `aset`, `alist`, `acount` and `aexists` (bounded by `max_items`, native SQL, owner row locked, `within=` scopes), `tags__name` filters and `prefetch_related("tags")`. Generated CRUD reads and writes the relation as a `<field>_ids` set (`tags_ids`; `ids_name=` renames it); `links=` (or `all_rows(Tag)`) is mandatory and scopes the targets, an out-of-scope id answers like a missing one (`422`), and a write replaces the set inside one transaction. See [many-to-many](docs/relations.md#many-to-many) and [many-to-many fields](docs/crud.md#many-to-many-fields). The admin edits the relation as a limited multi-select that respects the target admin's scope and the user's permissions ([admin](docs/admin-customize.md#many-to-many-fields)), and the development MCP's `inspect_project` describes it. Routes whose schemas carry an ids field run on the native generated-CRUD lane when `links=` entries are `all_rows(Target)` or `own_rows(Target, column)` (new: targets whose text `column` equals the caller's id, applied in the ids read and inside the write's locked transaction), reading the ids with one extra query per page and writing the bridge in the row's transaction; a custom scope callable, a hook or a `Linked`/`Unlinked` handler keeps the route on the Python lane, and the answers are the same. The reverse side of a relation applies the declaring side's `max_items`, both ends lock the declaring rows first (scoped by `within=`) and then the owner row, a refused write undoes itself in a savepoint even when the caller catches the error, links stored beyond `max_items` read as a server fault (`StoredLinksOverLimit`, `500`), and the owner row is locked and re-checked against its scope before the bridge is written (a row deleted meanwhile answers `404`, one taken out of scope `403`). `within=` is keyword-only on every `LinkSet` method; a many-to-many annotated without `ClassVar` is a registration error; `state_of`/`plan` include the generated bridges of the owners they are given; `prefetch_related` refuses unknown relation names; `Schema.from_model` reads prefetched ids into set fields.

#### 🔎 Filter, order and search generated lists
> `python/hinoki/filters.py`, `python/hinoki/resources.py`, `python/hinoki/runtime/binding.py`, `python/hinoki/runtime/rowplan.py`, `python/hinoki/orm/keyset.py`, `python/hinoki/pagination.py`, `python/hinoki/openapi.py`, `crates/bindings/src/filters.rs`

`crud(Invoice, path="/invoices", read=InvoiceOut, filters=InvoiceFilters)` makes the list accept the query parameters of a `hinoki.filters.FilterSet`: `Eq[T]` (`?paid=false`), `In[T]` (`?status=open,overdue`), `Range[T]` (`?issued.from=...&issued.to=...`), `?name.null=true` for nullable fields, `Search("reference", "customer_name")` (`?search=acme`) and an `ordering` allow-list (`?ordering=-issued`). Names, operators, types and values are checked in Rust; anything outside the declaration is a `422` with structured problems (`type`, `choice`, `unknown`, `operator`, `duplicate`, `too_long`, `too_many`) that never echo the client's value, and no client text becomes an SQL identifier. The filters are combined with AND on top of `get_queryset` and can only narrow it; the optional `filter_queryset(query, params, principal)` hook of the class form runs next, then the client's ordering (with the primary key as the last key) and pagination. A `CursorPagination` cursor is now bound to the client's filters and ordering (a different set answers `422 invalid`; unfiltered cursors stay valid). The list documents typed parameters in OpenAPI 3.1 and 3.2, with an `enum` for `ordering`. On Kiso, an offset-paginated filtered list without custom `get_queryset`/`filter_queryset` hooks runs on the native generated-CRUD list lane; cursor pagination and custom hooks stay on the Python lane. The wire path of a plain list now orders by the primary key only when the query has no order, as before; a client `ordering` always ends with it. A `Range[Decimal]` filter or an `ordering` entry naming a `Decimal` column is a named `422 unsupported` on SQLite (it stores a `Decimal` as text, which only compares by equality); a `FilterSet` field whose produced name collides with the pagination parameters (`page`/`page_size` or `cursor`/`limit`) is refused when the routes are declared, for `crud(...)` and for a manual action alike. See [filter, order and search the list](docs/crud.md#filter-order-and-search-the-list).

#### 📘 OpenAPI 3.2 and `query` as a first-class method
> `python/hinoki/openapi.py`, `python/hinoki/app.py`, `python/hinoki/settings.py`, `python/hinoki/ai/openapidiff.py`, `python/hinoki/ai/mcp.py`, `python/hinoki/ai/mcpapp.py`, `python/hinoki/docsui/`

The generated document is now OpenAPI `3.2.0`, and an `@http.query` action is written as the path item's `query` operation instead of the `x-query` extension. `Hinoki(openapi_version="3.1")` or `docs.openapi_version = "3.1"` keeps a `3.1.0` document with `x-query`; nothing else in the document differs. `hinoki openapi --breaking-against` treats `x-query` and `query` as the same operation, so a 3.1 baseline compares with a 3.2 document. The bundled viewers are now Swagger UI 5.35.1 and ReDoc 3.2.0, since the previous 5.17.14 and 2.1.5 refused a 3.2 document; ReDoc 3 is served as a JavaScript module. Some third-party tools read only 3.1 (`@apidevtools/swagger-parser` 13.1.0 rejects every 3.2 document), so set `openapi_version` to `3.1` for them. See [the document version](docs/openapi.md#the-document-version).

#### 📣 Typed events
> `python/hinoki/events.py`, `python/hinoki/cli/events.py`, `python/hinoki/project.py`, `python/hinoki/cli/project.py`, `python/hinoki/settings.py`, `python/hinoki/runtime/pipeline.py`, `python/hinoki/runtime/requests.py`, `python/hinoki/runtime/lifespan.py`, `python/hinoki/runtime/kiso.py`, `crates/kiso/src/metrics.rs`, `crates/bindings/src/metrics.rs`, `crates/bindings/src/kiso.rs`

`events.Handlers` registers handlers by event class: `@handlers.in_transaction(E)` (async, runs in the writer's transaction and can roll it back) and `@handlers.after_commit(E)` (runs after the outermost commit, failures are logged and never propagated). Applications list their collections in `Hinoki(events=[...])` or declare `events = "module:attribute"` per app in `hinoki.toml` (`manifest_events()`, `hinoki app add --events`; the module is yours to write, `hinoki app create` generates none). Order is deterministic; each handler has a timeout, and handlers per event and nested depth are bounded (`events` settings). `Created[M]`, `Updated[M]` and `Deleted[M]` are emitted by the model write paths (`acreate`, `asave`, the bulk methods, `adelete` and generated CRUD) only when a handler or `capture()` listens; an `in_transaction` handler makes the write transactional so it can roll it back, bulk and filter UPDATEs (`QuerySet.aupdate`) emit nothing, and a model with a handler for one of them leaves the native lane for the matching generated create, update or delete (reason `model_event_handlers`). Nothing registers by import and delivery stays inside one worker: no queue, no retry. `AuthRejected` (`missing`, `invalid` or `forbidden`, route template and method) and `RateLimited` (policy class name, route template, `retry_after`) are emitted when an access policy or a rate-limit policy refuses a request, from the one place each decision is made; they take `after_commit` handlers only (`in_transaction` is refused at registration), carry no credential, caller, address or raw path, cannot change the response, are bounded by `events.handler_timeout`, and cost nothing without a handler. While a handler listens for `AuthRejected`, Kiso leaves native authentication of those routes to Python (reason `auth_event_handlers` for generated CRUD). `hinoki events` lists the registered handlers, the limits and the apps' `events` targets. Worker lifecycle events are not included (use `on_startup` and `on_shutdown`). With `metrics.listen` on Kiso, every handler run is counted in `hinoki_events_total{event,phase,outcome}` and timed in `hinoki_event_handler_duration_seconds{event,phase}`; the series are fixed at startup from the registered handlers, so no label comes from a payload, and a bus without metrics pays one `None` check per run. See [Events](docs/events.md#authentication-and-rate-limit-events) and [Metrics](docs/metrics.md#event-handlers).

#### 🧱 Project-wide middleware and rules in `hinoki.toml`
> `python/hinoki/app.py`, `python/hinoki/project.py`, `python/hinoki/cli/project.py`, `python/hinoki/scaffold.py`

The `[project]` table takes two ordered lists of `module:attribute` entries, edited by hand (there is no command): `middleware = [...]` (each entry one `Middleware` or a list or tuple of them; the first listed runs outermost, with no dependency ordering) and `rules = [...]` (each entry a `RuleSet`; they are merged in listed order, and the same rule, with the same id and summary, listed by two entries is a `ProjectError` naming both). `manifest_middleware()` and the new `manifest_rules()` are the explicit forms, next to `manifest_events()`. `Hinoki()` loads routes, events, middleware and rules on its own for each of `include=`, `events=`, `middleware=` and `rules=` you leave out (an explicit value, even `()` or an empty `RuleSet()`, wins), and `hinoki check` and `hinoki mcp` use the app's rules when `--rules` is not given. Events stay per app. `hinoki new` writes `rules = ["<package>.rules:project_rules"]` and its `main.py` no longer passes `rules=`. A list that is not `module:attribute` strings, or a target that does not import or has the wrong type, is a `ProjectError`. The per-app `middleware` key, `hinoki app add --middleware` and the `middleware` field of `hinoki app list` are removed. See [Middleware](docs/middleware.md#declaring-middleware-in-hinokitoml) and [Rules](docs/rules.md#which-rules-run).

#### 🔌 WebSockets
> `crates/kiso/src/ws.rs`, `crates/kiso/src/wsrun.rs`, `crates/bindings/src/websocket.rs`, `crates/bindings/src/server/mod.rs`, `python/hinoki/websocket.py`, `python/hinoki/runtime/websockets.py`, `python/hinoki/runtime/asgisocket.py`, `python/hinoki/openapi.py`, `python/hinoki/settings.py`

`@http.websocket("/path/{param}")` declares a WebSocket action with `access=`, `message=` and `reply=` (`Schema` classes), `subprotocols=`, `origins=`, `max_message_bytes=` and `idle_timeout=`; the handler is an `async def` that takes an `http.WebSocket` and reads and sends text, binary or typed JSON messages. On Kiso the handshake, framing, masking, UTF-8 checks, ping and pong, the close handshake, message size limits, bounded queues and timers run in Rust; a refused handshake (`426`, `400`, `401`, `403`, an Origin that is not allowed, `503` over the connection limit) is an ordinary HTTP answer before the upgrade, and a server stop or reload closes open connections with `1001`. Granian serves the same actions through the ASGI `websocket` scope. New `execution` settings `max_websockets`, `ws_max_message_bytes`, `ws_queue`, `ws_ping_interval`, `ws_pong_timeout` and `ws_close_timeout`. The OpenAPI document describes the actions under `x-hinoki-websockets`. WebSocket over HTTP/2 and `permessage-deflate` are not provided. See [WebSockets](docs/websockets.md).

#### 🌿 A branch banner for the CLI
> `python/hinoki/terminal.py`, `python/hinoki/runtime/banner.py`, `python/hinoki/cli/new.py`, `python/hinoki/cli/shellui.py`

The banner of `hinoki new`, `hinoki app create`, the shell and Kiso at start is now a branch (`╭─`, `│`, `╰─`) titled `Hinoki` or `Kiso`, with the tagline and the version, then aligned key and value rows: Kiso's mode, workers, scheme, access log and native lane; the Python version, documentation and source for `hinoki new`; the application, settings, models and Python version for the shell. Without colour the output is the same plain line. See [`hinoki new`](docs/new-project.md#how-it-looks-in-a-terminal) and [Kiso](docs/kiso.md).

#### 🧭 Cursor (keyset) pagination
> `crates/orm/src/query.rs`, `crates/bindings/src/cursor.rs`, `crates/bindings/src/rows.rs`, `crates/bindings/src/db.rs`, `python/hinoki/pagination.py`, `python/hinoki/orm/keyset.py`, `python/hinoki/resources.py`, `python/hinoki/settings.py`

`pagination=CursorPagination()` pages generated lists and handwritten routes by position: the response carries signed, tamper-proof but readable (not encrypted) `next` and `previous` cursors that clients should treat as opaque, runs no `COUNT`, and costs the same at any depth when the sort columns are indexed. The keyset `WHERE`, the signature and, for generated lists, the JSON page are built in Rust. The cursor holds only a position, so `get_queryset`, ownership and tenant policies apply to every page; a tampered, foreign or expired cursor answers `422`. A secret of at least 32 bytes is required (`CursorPagination(secret=...)` or `pagination.cursor_secret`); `cursor_ttl_seconds` is optional. See [cursor pagination](docs/pagination.md#cursor-keyset-pagination).

#### 📣 Deprecated actions tell their callers
> `python/hinoki/runtime/deprecation.py`, `python/hinoki/runtime/binding.py`, `python/hinoki/runtime/pipeline.py`, `python/hinoki/runtime/servercore.py`, `python/hinoki/runtime/crudplan.py`, `python/hinoki/routing.py`, `python/hinoki/cli/routes.py`

`deprecated=True` or `deprecated=date(2026, 11, 22)` on an action (or on `crud(...)`) now also adds a `Deprecation` header to its responses and logs a warning on the `hinoki.deprecated` logger when it is called, at most once a minute per action. The action leaves the native lane (`lane_reason` `deprecated`).

```python
@http.get("/v1/tasks", deprecated=date(2026, 11, 22))
async def list_tasks_v1() -> list[str]: ...
```

#### 🏷️ Per-action OpenAPI controls
> `python/hinoki/routing.py`, `python/hinoki/openapi.py`, `python/hinoki/resources.py`, `python/hinoki/app.py`, `python/hinoki/ai/mcpapp.py`

`@http.get(..., include_in_schema=False)` leaves an action out of the OpenAPI document without changing how it is served or protected; `tags`, `deprecated` and `operation_id` describe it. `RouteGroup(...)` and `crud(...)` take the same controls, and a duplicate `operation_id` stops registration. MCP refuses an action that is left out.

```python
@http.get("/ops/reindex", access=admin_only, include_in_schema=False)
async def reindex() -> dict[str, str]: ...
```

#### 🔄 Reload on SIGHUP in Kiso
> `python/hinoki/runtime/supervisor.py`, `python/hinoki/runtime/kiso.py`, `python/hinoki/runtime/wsgiworker.py`, `python/hinoki/runtime/listener.py`, `python/hinoki/serve.py`, `python/hinoki/settings.py`

`kill -HUP` on the supervisor of `hinoki serve` replaces the workers one at a time on the same listening socket: a new worker imports the code and reads the settings again, reports once its lifespan has started and it serves, and only then is the old one stopped gracefully. A new worker that does not start keeps the old ones running and the reload stops with an error. New setting `server.sighup` (`"reload"`, the default, or `"stop"` for the earlier behavior; Kiso only). `hinoki serve` now runs a supervisor even with one worker unless `sighup` is `"stop"`; on Windows, which has no `SIGHUP`, nothing changes. A worker that is stopping now answers requests already sent to it instead of refusing them with `503`, and a replacement worker's metrics use the next free `worker` number. See [reload on `SIGHUP`](docs/kiso.md#reload-on-sighup).

#### ⚡ One-pass JSON bodies for plain schemas
> `crates/bindings/src/schema/typeddecode/mod.rs`, `crates/bindings/src/schema/mod.rs`, `python/hinoki/schema/__init__.py`, `python/hinoki/runtime/pipeline.py`

On Kiso, the JSON body of a `Schema` parameter without `pattern`, `normalize`, `one_of`, collections, `@before_validate` or checks is read by Rust straight into the instance's fields, without the intermediate dict and list tree. Any invalid or unusual body falls back to the ordinary decode and validation on the same bytes, so errors do not change. `Schema.validate_json(raw)` returns the instance, or `None` when the ordinary path must run. See [one-pass JSON body](docs/schema-validation.md#one-pass-json-body).

Schema fields now live in `__slots__`: they are no longer in `vars(instance)`, and a class that combines two bases that declare fields needs `slots=False` on all but one (a `RegistrationError` says so). Classes made with `model=` keep their fields in the instance dict. `Schema` now has a metaclass, so it cannot be combined with an `abc.ABC` or `Protocol` base.

The same plain schemas now construct (`Item(...)`), `validate`, `validate_text`, `replace` and `dump` in Rust, with the same results and errors; anything outside the plan runs the Python code. A `default_factory` whose value is rejected may be called twice. `validate` reads a plain dict straight into the slots, and `copy`, `deepcopy` and pickling of an instance run in Rust. See [native constructor and `validate`](docs/schema-validation.md#native-constructor-and-validate).

#### 📈 Prometheus metrics on Kiso
> `crates/kiso/src/metrics.rs`, `crates/kiso/src/scrape.rs`, `crates/bindings/src/metrics.rs`, `python/hinoki/settings.py`, `python/hinoki/serve.py`, `python/hinoki/runtime/supervisor.py`

Kiso can serve request metrics in the Prometheus text format from a dedicated listener (`metrics.listen`, off by default; Kiso only, and `hinoki serve --server granian` refuses it). Requests by route pattern, method and status class, duration histograms, requests in flight, response bytes and errors are counted in Rust with atomics; with several workers each keeps its series, told apart by a `worker` label. A `listen` address off the loopback needs `metrics.allow` or `metrics.token`. See [export Prometheus metrics](docs/metrics.md).

#### 🌐 Per-route CORS
> `python/hinoki/http/cors.py`, `python/hinoki/routing.py`, `python/hinoki/resources.py`, `python/hinoki/runtime/pipeline.py`, `python/hinoki/runtime/origins.py`, `python/hinoki/runtime/servercore.py`, `crates/bindings/src/server/mod.rs`

`@http.get(..., cors=http.Cors(origins=(...)))` gives one action its own CORS policy, which replaces the application's for that route; `RouteGroup(..., cors=...)` sets a default for its actions and `crud(..., cors=...)` for the generated ones. A preflight is answered for the route named by `Access-Control-Request-Method` and the path (Kiso decides it natively), without running `access`, a transaction or the handler. See [CORS per route](docs/web-security.md#cors-per-route).

#### 🗜️ Response compression on Kiso
> `crates/kiso/src/compress.rs`, `crates/kiso/src/encode.rs`, `crates/bindings/src/compression.rs`, `python/hinoki/settings.py`, `python/hinoki/runtime/kiso.py`, `python/hinoki/serve.py`

Kiso can compress response bodies with brotli or gzip. It is off by default and Kiso only: `hinoki serve --server granian` refuses `compression.enabled`. A new `compression` settings group (`enabled`, `encodings`, `min_size`, `types`, `event_streams`, `files`, `gzip_level`, `brotli_quality` (0 to 9), `max_concurrent`) controls it. Kiso negotiates `accept-encoding`, adds `Vary`, skips small bodies, other content types and answers with `cache-control: no-transform`, flushes a stream whenever its producer pauses, and sends an answer uncompressed when `max_concurrent` compressions are already running. The release wheel grows with the two new Rust crates (flate2 and brotli). See [compress responses](docs/compression.md).

#### 🌊 Streaming responses
> `python/hinoki/http/stream.py`, `python/hinoki/runtime/streaming.py`, `crates/bindings/src/streamlimit.rs`, `crates/bindings/src/filelocate.rs`, `crates/bindings/src/tally.rs`, `crates/bindings/src/disposition.rs`, `python/hinoki/runtime/kiso.py`, `python/hinoki/runtime/asgi.py`, `python/hinoki/testing.py`, `crates/kiso`, `crates/bindings`

An action annotated `-> Stream`, `-> EventStream` or `-> File` is sent while it is produced, on Kiso and on Granian: `Stream` for bytes or text from an async or plain iterable, `EventStream` and `Event` for server-sent events, and `File` for a file that Kiso sends without its bytes entering Python. A slow client slows the producer down, a client that leaves closes the source, and a stream that fails after the head is cut instead of ending as complete. New settings `execution.max_streams` (64), `execution.stream_idle_timeout` and `execution.sse_keepalive` (15 s). `atomic=True`, a generator handler and status `204`/`304` are refused at registration; `WSGIAdapter` refuses an application with a streamed action when it starts, and the application MCP endpoint will not expose one. `ASGIClient.stream()` and `SyncClient.stream()` read the pieces as they are sent, and `client.get()` stops with `BodyTooLarge` on a body that does not end. See [stream a response](docs/streaming.md).

#### 🧭 Create an app at the end of `hinoki new`
> `python/hinoki/cli/new.py`, `python/hinoki/cli/appprompts.py`, `python/hinoki/scaffold.py` (#405, #408, #410)

`hinoki new` creates a project without models. In an interactive terminal it now asks whether to create the first app and reuses the same code as `hinoki app create`; `--app NAME` creates it without asking and `--no-app` skips the question. Project and app names may contain single underscores.

#### 📍 Docs URL when the dev server starts
> `python/hinoki/serve.py`, `python/hinoki/runtime/supervisor.py` (#411)

`hinoki dev` prints the documentation URL next to the server address.

#### 🎨 Admin theme and logo
> `python/hinoki/settings.py`, `python/hinoki/admin/core.py`, `python/hinoki/admin/authviews.py` (#399, #421)

The `admin.theme` setting selects the default admin theme, and the admin logo is configurable.

#### 🖥️ Terminal banner
> `python/hinoki/runtime/banner.py`, `python/hinoki/terminal.py` (#412, #415)

The Hinoki box appears on every colour terminal and shows Kiso's startup in the `hinoki new` box.

---

### 🔧 Improvements

#### ⚡ Fewer allocations per native-lane response, and a one-pass response check
> `crates/kiso/src/headers.rs`, `crates/bindings/src/server/core.rs`, `crates/bindings/src/server/routes.rs`, `crates/bindings/src/server/prepared.rs`, `crates/bindings/src/request/pyapi.rs`, `crates/bindings/src/request/json.rs`, `python/hinoki/runtime/pipeline.py`, `python/hinoki/runtime/nativelane.py`

Response heads share the server's policy headers and move the answer's own headers instead of copying them, `header_dict` is filled directly for plain ASCII headers, and a route without a rate limit no longer hands its peer address to the request data. A route that returns `list[dict[str, object]]` with `validate_response=True` on the native lane is checked while Rust encodes it, so the list is walked once; any value the one-pass check cannot vouch for (subclasses, non-`str` keys, objects that could run Python code, encode errors) takes the previous two-step path, so the same `ResponseContractError` or encode error is raised and the bytes are identical. A route whose rows the one-pass check cannot vouch for (for example rows holding datetimes) stops using it after the first such response and keeps the previous behaviour. No API changes. On a loopback run on one machine, `/json-10k` served about 9% more requests per second; the other routes measured within noise. These are relative figures, not benchmark claims.

#### 🔗 One query per many-to-many prefetch chunk
> `python/hinoki/orm/links.py`, `python/hinoki/orm/queryset.py`, `crates/bindings/src/modelrows.rs`, `crates/orm/src/query.rs`, `docs/relations.md`

Many-to-many prefetch reads targets and their bridge in one bounded query per
owner chunk. On PostgreSQL, NULL now sorts before non-NULL values in ascending
order and after them in descending order even when targets fit in one chunk,
matching the order previously used when target reads spanned multiple chunks.

#### 🔐 One `Secret` type (breaking)
> `python/hinoki/fields.py`, `python/hinoki/settings.py`, `python/hinoki/crypto.py`, `python/hinoki/sessions.py`, `crates/bindings/src/pytypes.rs`, `docs/schema-types.md`, `docs/settings.md`

`SecretStr` is removed; `hinoki.Secret` (also `settings.Secret`) is the only secret type, for `Schema` fields, settings, `JWTKey(key=...)` and `SessionSettings(csrf_secret=...)`. Read it with `reveal()`; `repr` gives `Secret('**********')`, `str`, `dump()` and responses `**********`. Settings coerce text into a `Secret | None` field.

```python
from hinoki import Secret
from hinoki.crypto import JWTKey

key = JWTKey("2026-11", "HS256", Secret("from-a-secret-manager-at-least-32-bytes"))
```

#### 🧹 Public exports (breaking)
> `python/hinoki/orm/__init__.py`, `python/hinoki/db.py`, `python/hinoki/http/`, `python/hinoki/reply.py`

`hinoki.orm` no longer exports internals (`IN_CHUNK`, `MARK`, `OPERATORS`, `SEPARATOR`, `SNAPSHOT`, `FieldMeta`, `condition`, `database_var`, `fetch_in`); `hinoki.db` exports `AtomicScope` and `DatabaseModule`, `hinoki` exports `TextParser`, `hinoki.http` exports `set_cookie`, and the response header alias in `hinoki.http.response` is now `ResponseHeaders`.

#### 🧾 Wire forms at any depth (breaking)
> `crates/bindings/src/request/json.rs`, `crates/bindings/src/xml_encode.rs`, `docs/codecs.md`

The JSON and XML encoders write `Decimal`, `date`, `time`, `timedelta`, `bytes`, sets, IP addresses, paths, `Secret` and enum members in their wire form anywhere, including at the top level, instead of raising `TypeError` outside a `Schema`. A `JSONRenderer(default=...)` hook still sees such values first outside a `Schema`, except a `Secret`, which is always masked.

#### ⚡ Lifecycle models on the native CRUD lane
> `crates/bindings/src/crudplan.rs`, `crates/bindings/src/nativecrud.rs`, `crates/bindings/src/rows.rs`, `python/hinoki/runtime/crudplan.py`, `python/hinoki/runtime/rowplan.py`, `python/hinoki/runtime/servercore.py`, `python/hinoki/runtime/requests.py`, `python/hinoki/runtime/pipeline.py`, `python/hinoki/runtime/kiso.py`, `docs/kiso.md`

Generated `crud(...)` routes over `SoftDeleteModel`, `UUIDModel` (`uuid_pk()` with UUIDv4 or `version=7`), `urlsafe_token()` columns and `Decimal` or `date` columns now run on Kiso's native CRUD lane instead of falling back to Python. A soft delete stamps `deleted_at` (and `updated_at`) in Rust, and every native read only sees live rows, with the same SQL as the Python lane. A `Decimal` that Rust cannot render exactly as Python would, such as one Python writes in exponent form, is handed back to Python. `Enum` columns and `CursorPagination` lists still use the Python lane. With debug logging on, startup logs the reason each generated CRUD route stays on the Python lane.

The Python lane also does less work per request:

- row-wire list guards run once per worker start instead of on every request, and soft-delete lists use them;
- a JSON `Schema` body is validated in one Rust pass from the bytes;
- a request Kiso has already routed no longer repeats the host, CORS, redirect, probe and size checks in Python;
- native responses are moved rather than copied;
- the CRUD query string is parsed once.

#### ⚡ Native Kiso lane for filtered `crud(...)` lists
> `crates/bindings/src/filters.rs`, `crates/bindings/src/crudplan.rs`, `crates/bindings/src/nativecrud.rs`, `python/hinoki/runtime/crudplan.py`, `python/hinoki/core.pyi`, `docs/crud.md`, `docs/status.md`

Offset-paginated (`PageNumberPagination`) generated `crud(...)` `list` endpoints with a plain `FilterSet` and no custom `get_queryset`/`filter_queryset` hooks now run on Kiso's native Rust lane: query parsing, filter predicates (`Eq`, `In`, `Range`, `.null`, `Search`), dialect-aware `LIKE` escaping and stable primary-key tie-breaker ordering execute without invoking Python `FilterSet.parse`, while invalid requests (422), `CursorPagination`, custom hooks and SQLite `Decimal` filters fall back transparently to the Python lane. See [CRUD](docs/crud.md#filter-order-and-search-the-list).

#### ✨ A softer cursor glow on the homepage
> `theme/static/js/home.js`, `theme/static/css/home.css`

The hero glow now follows the cursor with a smaller, softer ellipse and a short eased motion that settles when the pointer stops. It stays visible over the IDE panel while keeping its code clear, fades on entry and exit, and remains off for coarse pointers and reduced-motion preferences.

#### 📦 Release build and package name
> `pyproject.toml`, `crates/bindings/src/lib.rs`, `.github/workflows/release-build.yml`, `scripts/smoke_wheel.py`, `python/hinoki/scaffold.py`, `python/hinoki/serve.py`, `docs/release.md`

The PyPI package is now named `hinoki-api` (the module is still `import hinoki`, the command still `hinoki`), because the `hinoki` name belongs to an unrelated project. Generated projects depend on `hinoki-api[server]` and name it under `[tool.uv.sources]`. A new `Release build` workflow builds manylinux x86_64/aarch64 and macOS arm64/x86_64 wheels and the sdist, smoke-tests each wheel and keeps them as artifacts; it publishes nothing. Windows is not supported natively: building the extension there stops with a message that points to WSL2. `docs/release.md` is the release checklist.

#### 🧾 XML responses written in Rust
> `crates/bindings/src/xml_encode.rs`, `python/hinoki/renderers.py`, `python/hinoki/core.pyi`, `docs/codecs.md`

`XMLRenderer` now writes through `core.encode_xml` instead of Python. It takes the same values as `JSONRenderer` and shares its depth limit (200) and its rules for UUIDs, datetimes, `Schema` instances and `dump()` objects. Behavior change: a float exponent is no longer zero-padded (`1e-7`, not `1e-07`). Everything else is byte-identical. `docs/codecs.md` now states the exact round-trip rules (`None`, `""`, `{}` and `[]` all read back as `None`).

#### ⚡ Less work per request
> `python/hinoki/schemafast.py`, `python/hinoki/schema/__init__.py`, `python/hinoki/runtime/pipeline.py`, `python/hinoki/runtime/binding.py`

A request now does less Python work with the same behaviour: a coroutine that finishes without suspending runs inline with no task and no timer, the `Request` object is built only when the action, a renderer or a body needs it, response header shapes are computed once per route shape, and a `Schema` whose fields hold exactly the declared types is accepted by a compiled per-class check instead of the full validation. A `Schema` that overrides `_validate_instance`, `native_schema` or `specs` keeps the full validation. Indicative single-core runs on a loaded host gave about +40% to +70% on a minimal action and +28% on a one-row read; the official comparative numbers are not yet re-measured.

On Kiso, an ORM read chain that uses `select_related(...)` now stays on the native handler lane, since the join is part of the same single query; `prefetch_related` still runs in an asyncio task (`python/hinoki/runtime/nativeproof.py`). An indicative single-core run of a one-row joined read gave about +14%.

#### 🔒 Hardened HTTP and WSGI edges
> `crates/kiso/src/conn.rs`, `crates/kiso/src/stream.rs`, `crates/bindings/src/wsgi.rs` (#389, #401, #402)

Middleware, raw responses and the WSGI path are stricter: a path with a NUL byte or a dot segment is refused, and a WSGI stream whose application goes silent is reset.

#### 🦀 Native page encoding and joined rows
> `crates/bindings/src/request/mod.rs`, `crates/bindings/src/schema/mod.rs`, `crates/bindings/src/modelrows.rs`, `python/hinoki/orm/queryset.py`

The JSON encoder writes a `Page` of `Schema` instances directly instead of building the dumped dicts first; a `Page` subclass or an item with a custom `dump` still goes through `dump()`. Encoding a `Schema` no longer raises and catches a `KeyError` per class lookup. Queries with `select_related(...)` now build the model and its joined instances in the native row builder whenever every involved model can already use it (no custom `from_row` or row hooks). The produced JSON and instances are unchanged. Indicative single-core runs gave about +21% on a paginated list of 20 rows, a further +4% on that list with a joined author, and +2% to +4% on a minimal action; the official comparative numbers are not yet re-measured.

#### 🗄️ Database defaults and messages
> `python/hinoki/db.py`, `crates/orm/src/database.rs` (#395, #397)

The database pool defaults to the settings size of 10, and a URL scheme that lacks its slashes is named in the error.

#### 🧮 JSON is read and written by the Rust core
> `python/hinoki/jsontext.py`, `crates/bindings/src/request/mod.rs`, `crates/bindings/src/modelrows.rs`

The command line, the migration state files, the JSON log format, the MCP server and the test client no longer use Python's `json` module. Output is the same data with compact separators (`{"a":1}`) where it was not indented, and floats such as `1e-07` print as `1e-7`. Command-line, log, MCP and migration-state output stays ASCII. What changes for callers:

- `NaN` and infinities are refused when written or read.
- Reading refuses more than 128 nested arrays or objects (the standard library allowed about 1000); this applies to MCP input and to the test client.
- The test client's `json=` also accepts `datetime` and `UUID` values.
- A JSON column whose bytes start with a UTF-8 byte order mark raises `FieldError`.

---

### 🐛 Fixes

- **Atomic stream rollback:** returning a streamed response (`Stream`, `EventStream`, or `File`) from an `atomic=True` action annotated `-> http.Response` now discards the stream and raises `ResponseContractError` inside the transaction block so the SQL transaction rolls back before translating the `500` response; intentional non-stream `Response` returns such as `409` still commit (`python/hinoki/runtime/pipeline.py`, `docs/transactions.md`, `docs/sessions.md`).
- **Typed `RouteGroup` decorator options:** `RouteGroup.get`, `post`, `put`, `patch`, `delete`, and `query` now expose the same typed keyword-only options as `http.*` instead of `**options: Any`, so type checkers reject mistyped or misspelled route options at check time (`python/hinoki/routing.py`, `docs/typing.md`, `docs/function-endpoints.md`).
- **OpenAPI names and decimals.** Security schemes, page envelopes and `Schema` classes that would share a component name get a numeric suffix instead of overwriting each other; page envelopes of scalar items are named after the item (`Page_integer`). `Decimal` bounds are written as `x-minimum`/`x-maximum`/`x-exclusiveMinimum`/`x-exclusiveMaximum` strings and decimal examples as text. `Field(description=..., examples=..., deprecated=..., discriminator=...)` used as a default now reaches the schema, and its overloads type-check as a default.
- **Kiso request heads:** blank lines before the request line now count toward `execution.max_header_bytes`, so the head bound matches what the HTTP parser reads (`crates/kiso/src/framing.rs`).
- **Admin forms and lists:** create and edit forms accept `date`, `time`, `Decimal` and `Enum` columns (a select of member names); required columns of these types no longer make creation fail. A soft-delete model with custom `tabs` keeps its Deleted tab, an admin at `Admin(prefix="")` links to `/`, the bento theme keeps Sign out and Change password on phones, the bulk bar works without JavaScript, and the paper and bento lists keep column sorting and select-all. Navigation links drop a stale success banner, the search form and tab bar are omitted when there is nothing to search or choose, and the admin UI reads its component and icon files on first use instead of at import (`python/hinoki/admin/`).
- **`hinoki admin`:** the command builds `AdminAccounts` from the project's `admin` settings, so `admin.min_password_length` applies to `create-superuser` and `set-password` (`python/hinoki/cli/admin.py`).
- **MCP, OpenAPI diff and rules:** `expose_as_mcp()` refuses an exposure that selects no allowed action, a streaming action or an action left out of the schema when it is built, not on the first call; `hinoki openapi --breaking-against` also reports a parameter that becomes required or changes type, a newly required request body and a dropped response body; generated `time` samples carry a UTC offset; `Schemas.forbid_exposure` reports a `dict` return as unverifiable (`python/hinoki/ai/`, `python/hinoki/rules.py`).
- **SQLite lock waits:** the SQLite writer-lock wait now follows a larger `Database.connect(wait=...)` value instead of always ending after five seconds; the default remains five seconds (`crates/orm/src/sqlite.rs`).
- **Long page indexes:** the sticky table of contents now scrolls within the viewport instead of extending over the footer (`theme/static/css/base.css`).
- **Security review fixes:** WebSocket handshakes over ASGI route on the raw path and honour `allowed_hosts` and trusted proxies, and the same-origin check compares the scheme (also on Kiso over HTTPS); a duplicate `Host` on HTTP/1.0 is refused; a non-numeric `Host` port no longer breaks WSGI; the admin form uses the admin timezone; documentation URLs are escaped; a full sign-in table keeps blocked addresses, sign-in and password change count apart, and a user name is delayed after repeated failures; MySQL `require_ssl` precedes a URL fragment; Kiso's WSGI host bounds the body bytes being read; a remote PostgreSQL URL without `sslmode` and a world-readable TLS key log a warning.
- **Manifests and scaffolds:** names and paths with characters above the Basic Multilingual Plane (for example emoji) are written as valid TOML; they used to be written as surrogate-pair escapes, which TOML rejects (`python/hinoki/projectcli.py`, `python/hinoki/scaffold.py`).
- **`hinoki check`:** it imports the manifest's models before running the rules and warns when it ran no rules (`python/hinoki/checkcli.py`, #391, #393).
- **Migrations:** a plan error no longer suggests running `migrations plan` (#392).
- **Model validation:** text length errors count characters, not items (`python/hinoki/orm/model.py`, #396).
- **WSGI:** a plain WSGI callable reads its settings from the environment (`python/hinoki/runtime/wsgiworker.py`, #400).
- **Admin secret error:** the error for an app with no admin secret names the real environment variable (for example `BLOG_ADMIN__SECRET`, from the settings class prefix) instead of a placeholder (`python/hinoki/admin/core.py`, `python/hinoki/app.py`).
- **`hinoki doctor`:** when the application cannot be built because of a Hinoki declaration error, the finding shows that error's message and a fix that points to it, instead of only the exception class (`python/hinoki/cli/doctor.py`, `docs/inspect.md`).
- **`hinoki routes`:** the handler column stays aligned when an access name is longer than 24 characters, in plain and coloured output (`python/hinoki/cli/routes.py`, `python/hinoki/cli/reportui.py`).
- **JSON nesting limits:** reading accepts exactly 128 nested arrays or objects and writing exactly 200, as documented; the 129th and the 201st are refused. An empty innermost array or object used to count as one more, and a `Schema` object is now counted as a container (`crates/bindings/src/request.rs`).

---

### 📚 Documentation

The docs site publishes `llms.txt`, `llms-full.txt` and a Markdown copy of every page at the site root and under each version, generated at build time by `docsite/llms.py` and checked in CI (`docs/ai-workflow.md`, `.github/workflows/deploy-docs.yml`).

The docsite has a new navigation, rules explorer, entry cards, footer and page transitions. The README is restructured around an animated request flow, the demo is rendered in HD at 60 fps, the comparative benchmark page is reorganized, and documentation links point at `hinoki.caspel.dev` (#388, #406, #409, #413, #416, #417, #419, #420, #423, #425, #426).

Kiso's fuzz workflow now covers request-line framing and request-head limits
with `head_framing` and `request_head_limits` (`fuzz/`,
`.github/workflows/fuzz.yml`, `docs/kiso.md`).

The [benchmarks page](docs/benchmarks.md) now shows a run on a dedicated 8 vCPU AMD EPYC server, with Fastify, Hono and `Bun.serve` as an extra group and the load average held at or below 2 before every sample. Hinoki in this run is commit `478e1452`.

The [benchmarks page](docs/benchmarks.md) is redesigned: 8 workers and 1 worker sit behind tabs, a Summary compares Hinoki on Kiso with the other stacks (ratios of medians, ties and unsettled results shown), and each route has its own table with the ratio, p50, p99, range and CV; the other runtimes stay a separate view (#529).

The README, the site description and the docs home now use the repository description, and the README's `Write an API` example follows the recommended syntax: `crud(...)` with `update` and `delete` in `routes.py` and a handwritten `@http.get` action. The footer credits MkDocs in a compact link (#531).

---

### 🎯 Summary

This is the running list of changes since the repository started. The first tagged release will be 0.1.0: `Unreleased` becomes `[0.1.0] - date`.
