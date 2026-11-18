# Concepts

This page connects the main parts of Hinoki. The guides explain each API in detail.

## A model is stored data; a Schema is API data

`Model` describes a database row and its queries (SQLite, PostgreSQL, MySQL or MariaDB). `Schema` validates the values an endpoint accepts or returns. A response schema should expose only the fields clients are meant to see. Model-derived `Schema` classes are a convenience, not a replacement for deciding that boundary.

## A declaration is reused

An action's Python signature and decorator options describe routing, binding, access, transaction behavior, expected errors, OpenAPI and MCP metadata. A route can be included as a standalone function, grouped in an `API` resource class, or collected in an includable `RouteGroup` of functions with shared defaults.

## One application object

A server process serves one `Hinoki` object, the one `hinoki serve module:app` or `project.application` in `hinoki.toml` names. Declare it at module level, as the generated project does:

```python
# blog/main.py
app = Hinoki(settings=BlogSettings, rules=blog_rules)     # routes come from hinoki.toml

@app.error_handler(stripe.CardError)                      # decorators on the app
def card_error(error: stripe.CardError) -> errors.APIError:
    return errors.APIError(error.user_message, status=402, code="card_declined")
```

Building `Hinoki(...)` declares and opens nothing, so importing the module is safe. Use a factory (`def create_app(...) -> Hinoki`, target `module:create_app`) when constructing the application needs arguments or conditions, such as the Blog's optional accounts or the Tracker's per-test database; inside a factory, register handlers with the same call after the app exists (`app.error_handler(Kind)(handler)`). The `[[apps]]` of `hinoki.toml` are something else: model and migration packages of the project, not further `Hinoki` objects.

## One request

The server passes an ASGI request through the Python adapter, the compiled router and binding layer. Hinoki validates inputs, resolves authentication and access, runs the handler, then encodes and renders its response. Runtime response values are not validated against their return annotations unless the action asks for it with [`validate_response=True`](endpoints.md#validate-the-response). For a transactional action, commit happens after successful response encoding and rendering; failures roll back before HTTP error translation. The ASGI boundary still crosses Python objects; the runtime is not an all-Rust server.

## Worker resources

Each server worker owns its module lifespan and database pool. Startup creates resources; shutdown closes them. A pool is not opened at import or shared across workers. Scripts outside the server manage their own short-lived connections.

## Optional rules

Rules inspect declared routes, schemas, imports or other known project structure. They report a violation or say they could not verify the promise. They do not prove arbitrary function behavior. Enable the checks that suit the application and run them both at startup and in development.

## Where to continue

* [Request lifecycle](request-lifecycle.md)
* [Transactions](transactions.md)
* [Sync and async](sync-async.md)
* [Architecture patterns](patterns.md)
