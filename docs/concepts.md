# Concepts

This page connects the main parts of Hinoki. The guides explain each API in detail.

## A model is stored data; a Schema is API data

`Model` describes a PostgreSQL row and its queries. `Schema` validates the values an endpoint accepts or returns. A response schema should expose only the fields clients are meant to see. Model-derived `Schema` classes are a convenience, not a replacement for deciding that boundary.

## A declaration is reused

An action's Python signature and decorator options describe routing, binding, access, transaction behavior, expected errors, OpenAPI and MCP metadata. A route can be included as a standalone function, grouped in an `API` resource class, or collected in an includable `RouteGroup` of functions with shared defaults.

## One request

The server passes an ASGI request through the Python adapter, the compiled router and binding layer. Hinoki validates inputs, resolves authentication and access, runs the handler, validates the response and renders it. For a transactional action, commit happens after successful response validation; failures roll back before HTTP error translation. The ASGI boundary still crosses Python objects; the runtime is not an all-Rust server.

## Worker resources

Each server worker owns its module lifespan and database pool. Startup creates resources; shutdown closes them. A pool is not opened at import or shared across workers. Scripts outside the server manage their own short-lived connections.

## Optional rules

Rules inspect declared routes, schemas, imports or other known project structure. They report a violation or say they could not verify the promise. They do not prove arbitrary function behavior. Enable the checks that suit the application and run them both at startup and in development.

## Where to continue

* [Request lifecycle](request-lifecycle.md)
* [Transactions](transactions.md)
* [Sync and async](sync-async.md)
* [Architecture patterns](patterns.md)
