---
status: implemented
related:
  - request-lifecycle.md
  - patterns.md
  - glossary.md
---

# Concepts

How the parts of Hinoki fit together. Each guide explains one part in detail.

## A model is stored data; a `Schema` is API data

A `Model` describes a table and its queries (SQLite, PostgreSQL, MySQL or MariaDB). A `Schema` validates what an endpoint accepts or returns. A response `Schema` exposes only the fields clients should see: `@derive(Invoice)` copies fields from a model, but you still decide the boundary, and a handler returns `InvoiceOut.from_model(row)`, never the row.

## A declaration is reused

An action's signature and decorator options state its route, inputs, access policy, transaction (`atomic=`), declared errors and response. Routing, binding, the OpenAPI document, the rules, the admin and MCP all read that one declaration. Actions are [plain functions](function-endpoints.md), optionally grouped in a `RouteGroup` with shared options, or [methods of an `API` class](class-endpoints.md). Each app lists them in its `routes.py`, and `hinoki.toml` mounts every app under its prefix ([register routes](routes.md)).

## One application object

A server process serves one `Hinoki` object: the one `project.application` in `hinoki.toml` names, or the target given to `hinoki serve`. Declare it at module level:

```python
# notes/main.py
import stripe
from hinoki import Hinoki, errors

from notes.rules import project_rules
from notes.settings import ProjectSettings

app = Hinoki(settings=ProjectSettings, title="Notes", rules=project_rules)


@app.error_handler(stripe.CardError)
def card_error(error: stripe.CardError) -> errors.APIError:
    return errors.APIError(error.user_message, status=402, code="card_declined")
```

`Hinoki(...)` includes the routes listed in `hinoki.toml` (`include=manifest_routes()`) and opens nothing, so importing the module is safe: commands, servers and tests use the same `app`. The `[[apps]]` of `hinoki.toml` are packages of models, migrations and routes, not further `Hinoki` objects.

## One request

The server hands each request to the compiled router and binding layer: Kiso, the default server, does so from Rust and queues matched requests for the Python pipeline; with Granian the request crosses the ASGI adapter first. Hinoki authenticates the caller, checks access, validates inputs, runs the handler, checks the return value against its annotation and renders the response. A transactional action commits after the response is prepared; a failure rolls back before it becomes an HTTP error. Handwritten handlers and hooks run in Python on both servers: the runtime is not an all-Rust server. Details: [request lifecycle](request-lifecycle.md).

## Worker resources

Each server worker owns its module lifespan and database pool: startup creates them, shutdown closes them. Nothing is opened at import or shared across workers. Scripts outside the server manage their own short-lived connections. See [modules](modules.md).

## Optional rules

Rules check declared routes, schemas, models and imports. Hinoki ships eleven; each reports a violation, a pass, or that it could not verify its promise. They do not prove what a function body does. Turn on the ones that suit the application and run them at startup and in development. See [rules](rules.md).

## See also

* [Request lifecycle](request-lifecycle.md)
* [Structure an application](patterns.md)
* [Sync and async code](sync-async.md)
* [Glossary](glossary.md)
