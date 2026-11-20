---
status: implemented
---

# Guides

One page per task. Start with data and endpoints, then add access, rules and runtime concerns as you need them. New to Hinoki? Take the [tutorial](tutorial/index.md) or the [quick start](quick-start.md) first.

## Data

- [Configure a database](databases.md): choose the backend, set the URL or connection fields, keep credentials secret.
- [Define models](models.md): columns, primary keys, column types, table names and write-time validators.
- [Model options](model-options.md): indexes, composite uniqueness, default order, update timestamps and names, as decorators.
- [Relations](relations.md): foreign keys, one-to-one, many-to-many association models and explicit loading.
- [Query and write rows](queries.md): filters, pagination, reusable queries, creates, updates and bulk writes.
- [Lifecycle hooks](model-hooks.md): react to a model's own writes and inspect its changed columns.
- [Migrations](migrations.md): plan, review, apply and check migrations for every app.
- [Schemas](schemas.md): declare the data that crosses the API boundary and convert it to and from rows.
- [Scalar field types](schema-types.md): enums, literals, dates, decimals, bytes, addresses, secrets and paths.
- [Collections and unions](schema-collections.md): sets, tuples, dictionaries, unions, discriminated unions, TypedDicts and dataclasses.
- [Validate input](schema-validation.md): constraints, normalization, field and object checks, and input reshaping.
- [Schemas from models](model-schemas.md): derive a `Schema` from a `Model` and convert rows with `from_model`.
- [Time and utilities](utilities.md): UTC time, the application timezone, test clocks and update timestamps.

## Endpoints

- [Function-based APIs](function-endpoints.md): the default pattern: plain functions grouped by a `RouteGroup` that states access, transactions and errors once.
- [Class-based APIs](class-endpoints.md): `API` classes for `@crud` resources with hooks, inheritance and resources you ship to others.
- [Register routes](routes.md): how `routes.py` and `hinoki.toml` build the route map, with `Mount`, `include=` in tests and `hinoki routes`.
- [Requests and responses](endpoints.md): what an action receives (path, query, body, forms, XML, files) and how its result is checked.
- [Generate CRUD routes](crud.md): list, retrieve and write actions from a model, with access and ownership rules.
- [Errors](errors.md): malformed requests, authentication failures and declared application errors.
- [Pagination](pagination.md): page-number pagination with `PageNumberPagination`.
- [Parsers and renderers](codecs.md): other body formats in and out.
- [OpenAPI document](openapi.md): the OpenAPI 3.1 document generated from routes, signatures and `Schema` declarations.

## Security and checks

- [Authentication and access](authentication.md): providers resolve a `Caller`; `Access` policies check permissions.
- [Hosts, CORS and HTTPS](web-security.md): restrict the host names an app answers to, which browser origins may read it, browser security headers, HTTPS redirects and trusted proxies.
- [Rules](rules.md): typed promises about your application, checked with a stable ID, a location and an explanation.

## Runtime

- [Application modules](modules.md): resources that live as long as a worker, such as a database pool.
- [Transactions](transactions.md): `atomic=True` on actions, `db.atomic` in application code.
- [Settings](settings.md): typed, immutable settings from the environment, dotenv and TOML, with redacted secrets.
- [Serve an application](serving.md): `hinoki dev` while you work, `hinoki serve` in production, on Kiso (the default) or Granian.
- [Kiso server](kiso.md): what Hinoki's Rust server runs natively, its limits, workers and signals, reload and reverse-proxy behaviour.
- [Test an application](testing.md): test through the whole stack with `ASGIClient`, against a disposable database, from the test `hinoki new` generates.
- [Command line](cli.md): every `hinoki` command, the short forms inside a project and the explicit forms outside one.
- [Create a project](new-project.md): the interactive `hinoki new` flow, its options and what the generated project contains.
- [Inspect an application](inspect.md): check rules, list routes, export the OpenAPI document, show settings and diagnose the environment.
- [Interactive shell](shell.md): an async-capable prompt with the app's settings and models.
- [Typing](typing.md): what a type checker catches in handlers, decorators and `Schema` construction.

## Integrations

- [Add an admin](admin.md): staff pages for your models, with a sign-in page, users and per-model permissions.
- [Customize the admin](admin-customize.md): columns, hooks, bulk actions, templates and themes.
- [Expose actions over MCP](app-mcp.md): selected actions as tools for a remote MCP client.
- [Development MCP](dev-mcp.md): a read-only server that describes your app to a coding agent.
- [Build with a coding agent](ai-workflow.md): a project, rules and tools ready for an agent, and where its authority ends.

## Operating

- [Sync and async code](sync-async.md): the event loop, the bounded sync pool and which style each surface accepts.
- [Benchmarks](benchmarks.md): retained measurements with their environment, method and limits.
- [Generated CRUD on the native path](rust-crud-orientation.md): a workload-specific measurement of generated CRUD.
- [Troubleshooting](troubleshooting.md): common errors by their message.
- [Toolchain and CI](toolchain.md): local and CI commands for contributors.

## Concepts and project

- [Concepts](concepts.md): how models, `Schema` classes, actions, workers and rules fit together.
- [Request lifecycle](request-lifecycle.md): the order of routing, access, binding, handler, commit and response.
- [Structure an application](patterns.md): layers, where each concern lives, and why.
- [Glossary](glossary.md): one word, one meaning.
- [Use other libraries](ecosystem.md): what owning `Schema` and `Model` means, and the extension points.
- [Reference](reference/index.md): public names, signatures and docstrings.
- [Status](status.md): what works today and what does not yet.
- [Documentation versions](documentation-versions.md): how `dev` and release docs are published.
