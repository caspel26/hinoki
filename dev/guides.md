# Guides

One page per task. Start with data and endpoints, then add access, rules and runtime concerns as you need them. New to Hinoki? Take the [tutorial](https://hinoki.caspel.dev/dev/tutorial/index.md) or the [quick start](https://hinoki.caspel.dev/dev/quick-start.md) first, or read the [examples](https://github.com/caspel26/hinoki/tree/main/examples) in order: Notes (easy), Tracker (medium) and Blog (complete), plus the focused Mailroom (outbound email).

## Data

- [Configure a database](https://hinoki.caspel.dev/dev/databases.md): choose the backend, set the URL or connection fields, keep credentials secret.
- [Define models](https://hinoki.caspel.dev/dev/models.md): columns, primary keys, column types, table names and write-time validators.
- [Model options](https://hinoki.caspel.dev/dev/model-options.md): indexes, composite uniqueness, default order, update timestamps and names, as decorators.
- [Relations](https://hinoki.caspel.dev/dev/relations.md): foreign keys, one-to-one, many-to-many association models and explicit loading.
- [Query and write rows](https://hinoki.caspel.dev/dev/queries.md): filters, pagination, reusable queries, creates, updates and bulk writes.
- [Lifecycle hooks](https://hinoki.caspel.dev/dev/model-hooks.md): react to a model's own writes and inspect its changed columns.
- [Events](https://hinoki.caspel.dev/dev/events.md): typed events with explicit handlers that run inside the transaction or after the commit.
- [Migrations](https://hinoki.caspel.dev/dev/migrations.md): plan, review, apply and check migrations for every app.
- [Schemas](https://hinoki.caspel.dev/dev/schemas.md): declare the data that crosses the API boundary and convert it to and from rows.
- [Scalar field types](https://hinoki.caspel.dev/dev/schema-types.md): enums, literals, dates, decimals, bytes, addresses, secrets and paths.
- [Collections and unions](https://hinoki.caspel.dev/dev/schema-collections.md): sets, tuples, dictionaries, unions, discriminated unions, TypedDicts and dataclasses.
- [Validate input](https://hinoki.caspel.dev/dev/schema-validation.md): constraints, normalization, field and object checks, and input reshaping.
- [Schemas from models](https://hinoki.caspel.dev/dev/model-schemas.md): derive a `Schema` from a `Model` and convert rows with `from_model`.
- [Time and utilities](https://hinoki.caspel.dev/dev/utilities.md): UTC time, the application timezone, test clocks and update timestamps.

## Endpoints

- [Function-based APIs](https://hinoki.caspel.dev/dev/function-endpoints.md): the default pattern: plain functions grouped by a `RouteGroup` that states access, transactions and errors once.
- [Class-based APIs](https://hinoki.caspel.dev/dev/class-endpoints.md): `API` classes for `@crud` resources with hooks, inheritance and resources you ship to others.
- [Register routes](https://hinoki.caspel.dev/dev/routes.md): how `routes.py` and `hinoki.toml` build the route map, with `Mount`, `include=` in tests and `hinoki routes`.
- [Requests and responses](https://hinoki.caspel.dev/dev/endpoints.md): what an action receives (path, query, body, forms, XML, files) and how its result is checked.
- [Store uploaded files](https://hinoki.caspel.dev/dev/storage.md): bounded memory or local-disk storage with opaque keys and streamed downloads.
- [Generate CRUD routes](https://hinoki.caspel.dev/dev/crud.md): list, retrieve and write actions from a model, with access and ownership rules.
- [Errors](https://hinoki.caspel.dev/dev/errors.md): malformed requests, authentication failures and declared application errors.
- [Stream a response](https://hinoki.caspel.dev/dev/streaming.md): `Stream`, `EventStream` and `File` answers sent while they are produced, on Kiso and Granian.
- [WebSockets](https://hinoki.caspel.dev/dev/websockets.md): `@http.websocket` actions with `Access`, typed JSON messages, limits and back-pressure, on Kiso and Granian.
- [Compress responses](https://hinoki.caspel.dev/dev/compression.md): optional brotli and gzip compression of bodies, on Kiso only.
- [Export Prometheus metrics](https://hinoki.caspel.dev/dev/metrics.md): request counters and histograms on a dedicated listener, on Kiso only.
- [Middleware](https://hinoki.caspel.dev/dev/middleware.md): typed `Middleware` around an action or the whole app, identical on Kiso and Granian.
- [Pagination](https://hinoki.caspel.dev/dev/pagination.md): page-number pagination with `PageNumberPagination` and keyset cursors with `CursorPagination`.
- [Filter, order and search a CRUD list](https://hinoki.caspel.dev/dev/crud.md#filter-order-and-search-the-list): `FilterSet` with `Eq`, `In`, `Range`, `Search` and an ordering allow-list.
- [Parsers and renderers](https://hinoki.caspel.dev/dev/codecs.md): other body formats in and out.
- [OpenAPI document](https://hinoki.caspel.dev/dev/openapi.md): the OpenAPI 3.2 document (3.1 on request) generated from routes, signatures and `Schema` declarations.

## Security and checks

- [Authentication and access](https://hinoki.caspel.dev/dev/authentication.md): providers resolve a `Caller`; `Access` policies check permissions.
- [Application sessions](https://hinoki.caspel.dev/dev/sessions.md): server-side state, cookie lifecycle, CSRF checks, session authentication and explicit stores.
- [Hosts, CORS and HTTPS](https://hinoki.caspel.dev/dev/web-security.md): restrict the host names an app answers to, which browser origins may read it, browser security headers, HTTPS redirects and trusted proxies.
- [Rules](https://hinoki.caspel.dev/dev/rules.md): typed promises about your application, checked with a stable ID, a location and an explanation.

## Runtime

- [Application modules](https://hinoki.caspel.dev/dev/modules.md): resources that live as long as a worker, such as a database pool.
- [Cache values](https://hinoki.caspel.dev/dev/cache.md): bounded Rust memory cache or a shared database backend with explicit TTL and failure behavior.
- [Transactions](https://hinoki.caspel.dev/dev/transactions.md): `atomic=True` on actions, `db.atomic` in application code.
- [Settings](https://hinoki.caspel.dev/dev/settings.md): typed, immutable settings from the environment, dotenv and TOML, with redacted secrets.
- [Serve an application](https://hinoki.caspel.dev/dev/serving.md): `hinoki dev` while you work, `hinoki serve` in production, on Kiso (the default) or Granian.
- [Kiso server](https://hinoki.caspel.dev/dev/kiso.md): what Hinoki's Rust server runs natively, its limits, workers and signals, reload and reverse-proxy behaviour.
- [Test an application](https://hinoki.caspel.dev/dev/testing.md): test through the whole stack with `ASGIClient`, against a disposable database, from the test `hinoki new` generates.
- [Command line](https://hinoki.caspel.dev/dev/cli.md): every `hinoki` command, the short forms inside a project and the explicit forms outside one.
- [Create a project](https://hinoki.caspel.dev/dev/new-project.md): the interactive `hinoki new` flow, its options and what the generated project contains.
- [Inspect an application](https://hinoki.caspel.dev/dev/inspect.md): check rules, list routes, export the OpenAPI document, show settings and diagnose the environment.
- [Interactive shell](https://hinoki.caspel.dev/dev/shell.md): an async-capable prompt with the app's settings and models.
- [Typing](https://hinoki.caspel.dev/dev/typing.md): what a type checker catches in handlers, decorators and `Schema` construction.

## Integrations

- [Add an admin](https://hinoki.caspel.dev/dev/admin.md): staff pages for your models, with a sign-in page, users and per-model permissions.
- [Customize the admin](https://hinoki.caspel.dev/dev/admin-customize.md): columns, hooks, bulk actions, templates and themes.
- [Send email](https://hinoki.caspel.dev/dev/email.md): SMTP, console and memory backends from a per-worker mailer, sent after a commit; no queue.
- [Email templates](https://hinoki.caspel.dev/dev/email-templates.md): typed TOML templates rendered in Rust, checked at startup and previewed from the CLI.
- [Expose actions over MCP](https://hinoki.caspel.dev/dev/app-mcp.md): selected actions as tools for a remote MCP client.
- [Development MCP](https://hinoki.caspel.dev/dev/dev-mcp.md): a server that describes your app to a coding agent; it can run requests against the app only when you enable `--scenarios`.
- [Build with a coding agent](https://hinoki.caspel.dev/dev/ai-workflow.md): a project, rules and tools ready for an agent, the llms.txt files and Markdown mirrors of these docs, and where its authority ends.

## Operating

- [Sync and async code](https://hinoki.caspel.dev/dev/sync-async.md): the event loop, the bounded sync pool and which style each surface accepts.
- [Benchmarks](https://hinoki.caspel.dev/dev/benchmarks.md): retained measurements with their environment, method and limits.
- [Changelog](https://hinoki.caspel.dev/dev/changelog.md): every user-facing change, explorable by version and type.
- [Native CRUD orientation run](https://hinoki.caspel.dev/dev/rust-crud-orientation.md): a workload-specific measurement of generated CRUD.

## Concepts and project

- [Concepts](https://hinoki.caspel.dev/dev/concepts.md): how models, `Schema` classes, actions, workers and rules fit together.
- [Request lifecycle](https://hinoki.caspel.dev/dev/request-lifecycle.md): the order of routing, access, binding, handler, commit and response.
- [Structure an application](https://hinoki.caspel.dev/dev/patterns.md): layers, where each concern lives, and why.
- [Glossary](https://hinoki.caspel.dev/dev/glossary.md): one word, one meaning.
- [Use other libraries](https://hinoki.caspel.dev/dev/ecosystem.md): what owning `Schema` and `Model` means, and the extension points.
- [Reference](https://hinoki.caspel.dev/dev/reference/index.md): public names, signatures and docstrings.
- [Status](https://hinoki.caspel.dev/dev/status.md): what works today and what does not yet.
- [Documentation versions](https://hinoki.caspel.dev/dev/documentation-versions.md): how `dev` and release docs are published.
- [Troubleshooting](https://hinoki.caspel.dev/dev/troubleshooting.md): common errors by their message.
- [Toolchain and CI](https://hinoki.caspel.dev/dev/toolchain.md): local and CI commands for contributors.
- [Release checklist](https://hinoki.caspel.dev/dev/release.md): build, check and publish a release.
