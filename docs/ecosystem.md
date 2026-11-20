---
status: implemented
---

# Own schemas and ORM: what it means for integrations

Hinoki has its own `Schema` and `Model` classes. It does not accept Pydantic models, SQLAlchemy models or Django models as request bodies, responses or tables. This page says why, what that costs when you bring existing libraries, and which extension points exist today. It describes what is implemented; it is not a plugin roadmap.

## Why the framework owns them

- **One declaration feeds every output.** A `Schema` and a `Model` are read by request binding, response checking, OpenAPI, application MCP, the admin and [rules](rules.md). Those tools can only promise the same contract everywhere if they read the same classes.
- **The hot paths are native.** Routing, selected request parsing and ORM execution run in Rust against Hinoki's own declarations, so they do not call back into a validation library or a foreign ORM per request.
- **Rules need known structure.** A rule such as "a response `Schema` never exposes `password_hash`" is checkable because the schema is a Hinoki class that can be inspected, not arbitrary validation code.

## What this costs

- A library that expects its own model type does not plug in directly: a Pydantic-based client SDK, a SQLAlchemy extension or a Django app that needs a Django `Model` cannot be passed a Hinoki `Model` or `Schema`.
- There is no adapter that converts SQLAlchemy, Django or Pydantic models into Hinoki ones, and no package registry or plugin system.
- Supported databases are SQLite, PostgreSQL, MySQL and MariaDB through Hinoki's own drivers. Your existing ORM session or connection is not shared with Hinoki's pool.

## Using other libraries anyway

Anything that works on plain Python data works with Hinoki. `Schema.to_dict()` and `Schema.dump()` give plain dictionaries, and a `Schema` is built from plain values, so the boundary with another library is ordinary Python:

```python
from hinoki import Schema, http


class Quote(Schema):
    symbol: str
    price: float


@http.get("/quotes/{symbol}")
async def quote(symbol: str) -> Quote:
    raw = await pricing_client.fetch(symbol)  # any library, returning plain data
    return Quote(symbol=raw["symbol"], price=raw["price"])
```

Keep the translation at the edge of the handler and keep your own database access (for a second store, for example) in a [module](modules.md) so its lifetime follows the worker. Hinoki does not protect or time-box what that code does.

## Extension points that exist

| You want to | Use | Guide |
| --- | --- | --- |
| Add an authentication scheme | Subclass `BearerAuth`, `APIKeyAuth` or `HeaderAuth` | [Authentication](authentication.md#define-a-provider) |
| Reuse a permission rule | A typed `Access` policy | [Authentication](authentication.md) |
| Accept or answer another body format | A parser or renderer | [Parsers and renderers](codecs.md) |
| Translate a library's exception | `app.error_handler(Kind)` | [Errors](errors.md) |
| Own a resource per worker | A module with `ctx.provide` and `ctx.get`, or a subclass that replaces `DatabaseModule` | [Modules](modules.md) |
| Enforce a team convention | Subclass `hinoki.rules.Rule` | [Rules](rules.md#your-own-rules) |
| Add typed configuration | `settings.AppSettings` | [Settings](settings.md) |
| Add routes from a package | Anything `include` accepts: an `API` class, a function, a `RouteGroup`, a `Mount` or an object with an `api()` method | [Endpoints](endpoints.md) |
| Expose actions to an agent | `expose_as_mcp` with an explicit allowlist | [Application MCP](app-mcp.md) |
| Add metrics, tracing or other cross-cutting behavior | Wrap the ASGI application | [Serving](serving.md#metrics-and-tracing) |
| Run where only WSGI is available | `WSGIAdapter` | [Serving](serving.md#wsgi) |

There is no middleware API of Hinoki's own: cross-cutting behavior outside a request's declared access, errors and transactions belongs in an ASGI wrapper, which sees the request before and after Hinoki. Hinoki also has no documented way to host another web application inside it; put the other service beside it behind a reverse proxy, or dispatch by path in your own ASGI code.

## Where to continue

* [Concepts](concepts.md)
* [Project status](status.md)
* [Glossary](glossary.md)
