---
status: implemented
related:
  - request-lifecycle.md
  - kiso.md
  - serving.md
  - transactions.md
---

# Add middleware

A middleware runs around an action. It is Hinoki's own typed interface, so it
behaves the same on Kiso and on Granian: you do not wrap the ASGI application,
and an application served by Kiso can use it.

```python
# notes/middleware.py
from hinoki.http import Middleware, MiddlewareRequest, Next, Response


class Tenant(Middleware):
    async def __call__(self, request: MiddlewareRequest, call_next: Next) -> Response:
        if "x-tenant" not in request.headers:
            return Response(400, "missing x-tenant", content_type="text/plain")
        response = await call_next()
        return response.with_header("x-served-by", "tenant")
```

Declare it on an action, or for the whole application. `middleware=` on
`Hinoki(...)` is the explicit form; a project lists it in `hinoki.toml`
instead ([below](#declaring-middleware-in-hinokitoml)):

```python
# notes/main.py
from hinoki import Hinoki, Schema, http

from .middleware import Tenant


class Pong(Schema):
    ok: bool


@http.get("/ping")
async def ping() -> Pong:
    return Pong(ok=True)


app = Hinoki(include=[ping], middleware=[Tenant()])
```

`RouteGroup` actions take the same option. The application's middleware runs
first, then the action's, in the order listed; each one returns what the next
one returned unless it answers itself.

## Declaring middleware in `hinoki.toml`

The `[project]` table can list middleware for the whole application. Hinoki does not generate the module: you create it
yourself, and you edit the list by hand (there is no command for it). The
Tracker example does this with one middleware that adds a version header
([`middleware.py`](https://github.com/caspel26/hinoki/blob/main/examples/tracker/tracker/middleware.py),
listed on line 4 of its
[`hinoki.toml`](https://github.com/caspel26/hinoki/blob/main/examples/tracker/hinoki.toml)).

```python
# shop/middleware.py
tenant = Tenant()
timing = [Timing(), Audit()]  # a list or tuple is flattened
```

```toml
[project]
application = "shop.main:app"
middleware = ["shop.middleware:tenant", "shop.middleware:timing"]
```

```python
from hinoki import Hinoki

app = Hinoki()  # reads routes, events, middleware and rules from hinoki.toml
```

Each entry is `module:attribute` and resolves to one object with an
`async def __call__` or a list or tuple of them. The listed order is the
execution order: the first one listed runs outermost. There is no dependency
ordering. When the manifest's `project.application` names the module that
builds the app, `Hinoki()` loads the list if you did not pass `middleware=`; an
explicit `middleware=` (even `()`) wins and skips the manifest.
`manifest_middleware()` is the explicit form, to combine lists. The result is
the application's own middleware: it runs before an action's and obeys
[the order below](#order-and-what-stays-outside). `project.middleware` that is
not a list of `module:attribute` strings is rejected when the manifest loads,
and a target that does not import or does not hold middleware raises a
`ProjectError` naming the entry.

## What a middleware sees

`MiddlewareRequest` is the head of the request: `method`, `path`, the matched
route template in `route` (for example `/items/{item_id}`, safe to use as a
metrics label), lower-case `headers`, the raw `query` string, `client`,
`scheme` and `request_id`. It has no body and cannot change the request:
`headers` is a read-only mapping, so authentication and the action see the
headers the client sent.

```python
class Forge(Middleware):
    async def __call__(self, request: MiddlewareRequest, call_next: Next) -> Response:
        request.headers["authorization"] = "Bearer x"  # TypeError: a 500
        return await call_next()
```

A middleware that needs to pass data on should answer itself or add response
headers instead.

The `Response` a middleware returns is checked like one a handler returns,
also when it is built with `Response.raw(status, body, headers)` from encoded
parts: a framing or server header (`content-length`, `transfer-encoding`,
`connection`, `x-request-id`), an invalid name or a line break in a value is a
`ValueError`, so the request answers `500` instead of a corrupt response.

`call_next()` returns a `Response` with the action's answer. An error the action
raised is already an HTTP answer, and an `atomic=True` transaction is already
rolled back, so a middleware that reads the database after `call_next()` sees
the state after the rollback. Call `call_next()` at most once (a second call is
a `500`). A middleware that raises is translated like an action error; one that
returns anything but a `Response` is a `500`.

For an action that returns `Stream`, `EventStream` or `File`, `call_next()` returns that object with an empty `body`: a middleware never sees the pieces, and `with_header` keeps the class and the source. A middleware that answers with another `Response` instead of the stream does not serve it, and Hinoki closes its source. See [stream a response](streaming.md#middleware).

Security, CORS and request-id headers are added after the middleware returns.

## Order and what stays outside

1. Hinoki answers what needs no action first: the ready check, request size
   limits, host, CORS preflight and HTTPS redirect policies, the startup probe
   and the documentation pages.
2. The route is chosen. An unknown path (`404`) or method (`405`) never reaches
   a middleware.
3. The application's middleware, then the action's.
4. Authentication, rate limit, negotiation, body binding, the transaction and
   the handler.

`execution.request_timeout` applies to the whole of it: when it expires the
`504` is produced outside the middleware, which only sees the cancellation.

## Cost

An action with middleware is served by Python for the whole request, so it
leaves [the native lane](kiso.md#what-is-native-and-what-still-runs-in-python):
`hinoki routes --lanes` shows `lane_reason` `middleware`. A deprecated action
([deprecate an action](openapi.md#deprecate-an-action)) leaves the lane the same
way, with `lane_reason` `deprecated`. Actions without
middleware keep their lane. A middleware that only needs to observe requests can
use the [access log](kiso.md#the-native-handler-lane) instead, which does not
change the lane.

## Errors and edge cases

- A middleware must have `async def __call__`; any other object is a `TypeError`
  when the application or the action is declared.
- Keep a middleware free of request state kept in globals: use the
  `MiddlewareRequest` it receives.

## See also

- [Request lifecycle](request-lifecycle.md)
- [Serve an application](serving.md)
- [Transactions](transactions.md)
