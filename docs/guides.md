---
status: implemented
---

# Guides

One page for each part of an application. Start with the data and endpoint guides, then add access, rules and runtime concerns as you need them. New to Hinoki? Take the [tutorial](tutorial/index.md) first, or follow the [quick start](quick-start.md).

## Data

- [Models and queries](models.md): Models map Python classes to database tables, with typed queries and opt-in write-time field validation.
- [Configure a database](databases.md): Set the backend URL, install the database module and apply migrations safely.
- [Schemas and validation](schemas.md): A Schema describes API data, with typed validation, normalization, rejection checks and class-level unknown-field policies.
- [Model-derived schemas](model-schemas.md): A Schema can take its fields and constraints from a Model.
- [Model options](model-options.md): Options that describe a model beyond its columns are decorators, read like the action decorators.
- [Utilities](utilities.md): Hinoki includes a small set of helpers where the framework can define clear behavior.
- [Relations](relations.md): Foreign keys, explicit many-to-many association models, and how to load related rows.
- [Migrations](migrations.md): Hinoki uses model declarations and recorded migration state to write SQL migration files.

## Endpoints

- [Endpoint declarations](endpoints.md): Hinoki endpoints are ordinary Python functions decorated with an HTTP method.
- [Generate CRUD routes](crud.md): Generate model-backed list, retrieve and selected write actions, with access and ownership rules.
- [OpenAPI](openapi.md): The OpenAPI 3.1 document is generated from registered routes, signatures and schema declarations.
- [Errors](errors.md): Hinoki separates malformed requests, authentication failures and application errors.
- [Pagination](pagination.md): An action opts into page-number pagination with `PageNumberPagination()`.
- [Parsers and renderers](codecs.md): A parser turns a request body into Python data; a renderer turns a handler's result into response bytes.

## Security and checks

- [Authentication and access](authentication.md): Hinoki keeps credential parsing in an authentication provider and permission checks in an Access policy.
- [Rules](rules.md): Rules are optional, typed promises about your application that a tool checks and that fail with a stable ID, a location and an explanation.

## Runtime

- [Application modules](modules.md): Modules own resources whose lifetime belongs to a worker, such as a database pool.
- [Transactions](transactions.md): An action opts into a database transaction with transaction=True.
- [Settings](settings.md): Typed, immutable settings classes with environment, dotenv and TOML sources, and redacted secrets.
- [Serving](serving.md): Run an application with `hinoki dev` while you work and `hinoki serve` in production-style runs.
- [Testing](testing.md): Hinoki has two useful levels of application tests: call the operation functions directly, then exercise the app through its ASGI interface.
- [Command line](cli.md): Run framework tools and app-defined management commands from the project directory.
- [Interactive shell](shell.md): Explore app settings and models with a persistent, async-capable Python prompt.
- [Typing](typing.md): Hinoki's public APIs use Python annotations so a type checker can catch mistakes in handlers, decorators and schema construction.

## Integrations

- [Admin](admin.md): An optional module that gives staff a list, search, create, edit, bulk delete and a dashboard for your models; it is included in the framework package and mounted explicitly.
- [Application MCP](app-mcp.md): expose_as_mcp turns selected HTTP actions into tools for an MCP client.
- [Development MCP](dev-mcp.md): CLI-hosted tools describe an app and its rules to an agent.
- [Development tools](ai-workflow.md): Scaffold a project and give a coding agent rules, error explanations and an MCP server.

## Operating

- [Sync and async](sync-async.md): Hinoki runs on one event loop per worker; synchronous handlers run on a bounded worker pool.
- [Benchmarks](benchmarks.md): retained measurements with their environment, method and limits.

## Where to go next

- [Concepts](concepts.md): the request lifecycle, architecture patterns and the vocabulary.
- [Reference](reference/index.md): public symbols, signatures and docstrings.
- [Status](status.md): what works today and what does not yet.
