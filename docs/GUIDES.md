---
status: implemented
---

# Guides

One page for each part of an application. Start with the data and endpoint guides, then add access, rules and runtime concerns as you need them. New to Hinoki? Take the [tutorial](tutorial/index.md) first, or follow the [quick start](QUICK-START.md).

## Data

- [Models and queries](MODELS.md): Model maps a Python class to one PostgreSQL table.
- [Schemas](SCHEMAS.md): A Schema describes data that crosses the API boundary and validates it strictly.
- [Model-derived schemas](MODEL-SCHEMAS.md): A Schema can take its fields and constraints from a Model.
- [Model options](MODEL-OPTIONS.md): Options that describe a model beyond its columns are decorators, read like the action decorators.
- [Utilities](UTILITIES.md): Hinoki includes a small set of helpers where the framework can define clear behavior.
- [Relations](RELATIONS.md): Foreign keys, explicit many-to-many association models, and how to load related rows.
- [Migrations](MIGRATIONS.md): Hinoki uses model declarations and recorded migration state to write SQL migration files.

## Endpoints

- [Endpoint declarations](ENDPOINTS.md): Hinoki endpoints are ordinary Python functions decorated with an HTTP method.
- [OpenAPI](OPENAPI.md): The OpenAPI 3.1 document is generated from registered routes, signatures and schema declarations.
- [Errors](ERRORS.md): Hinoki separates malformed requests, authentication failures and application errors.
- [Pagination](PAGINATION.md): An action opts into page-number pagination with `PageNumberPagination()`.
- [Parsers and renderers](CODECS.md): A parser turns a request body into Python data; a renderer turns a handler's result into response bytes.

## Security and checks

- [Authentication and access](AUTHENTICATION.md): Hinoki keeps credential parsing in an authentication provider and permission checks in an Access policy.
- [Rules](RULES.md): Rules are optional, typed promises about your application that a tool checks and that fail with a stable ID, a location and an explanation.

## Runtime

- [Application modules](MODULES.md): Modules own resources whose lifetime belongs to a worker, such as a database pool.
- [Transactions](TRANSACTIONS.md): An action opts into a database transaction with transaction=True.
- [Settings](SETTINGS.md): Typed, immutable settings classes with environment, dotenv and TOML sources, and redacted secrets.
- [Serving](SERVING.md): Run an application with `hinoki dev` while you work and `hinoki serve` in production-style runs.
- [Testing](TESTING.md): Hinoki has two useful levels of application tests: call the operation functions directly, then exercise the app through its ASGI interface.
- [Command line](CLI.md): The hinoki command accepts a module:attribute target for an application, factory, settings class or model module as documented by each subcommand.
- [Typing](TYPING.md): Hinoki's public APIs use Python annotations so a type checker can catch mistakes in handlers, decorators and schema construction.

## Integrations

- [Admin](ADMIN.md): An optional extra that gives staff a list, search, create, edit, bulk delete and a dashboard for your models.
- [Application MCP](APP-MCP.md): expose_as_mcp turns selected HTTP actions into tools for an MCP client.
- [Development MCP](DEV-MCP.md): CLI-hosted tools describe an app and its rules to an agent.
- [Development tools](AI-WORKFLOW.md): Scaffold a project and give a coding agent rules, error explanations and an MCP server.

## Operating

- [Sync and async](SYNC-ASYNC.md): Hinoki runs on one event loop per worker; synchronous handlers run on a bounded worker pool.

## Where to go next

- [Concepts](CONCEPTS.md): the request lifecycle, architecture patterns and the vocabulary.
- [Reference](reference/index.md): public symbols, signatures and docstrings.
- [Status](STATUS.md): what works today and what does not yet.
