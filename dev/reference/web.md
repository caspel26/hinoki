# Core API: application, routing and HTTP

`from hinoki import Hinoki, RouteGroup, Mount, http, errors` and `from hinoki.project import manifest_routes`. Guides: [function-based APIs](https://hinoki.caspel.dev/dev/function-endpoints.md), [class-based APIs](https://hinoki.caspel.dev/dev/class-endpoints.md), [register routes](https://hinoki.caspel.dev/dev/routes.md), [errors](https://hinoki.caspel.dev/dev/errors.md).

- `hinoki.app.Hinoki(...)`: A Hinoki application. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.app.Hinoki)

- `hinoki.api.API()`: Base class for a resource: several methods, standard or custom. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.api.API)

- `hinoki.routing.RouteGroup(...)`: A function-based route group with a shared path prefix and defaults. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.routing.RouteGroup)

- `hinoki.routing.Includable`: Type alias. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.routing.Includable)

- `hinoki.routing.Mount(prefix: 'str', *items: 'object') -> 'None'`: Places routes under a path prefix chosen where the application is declared. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.routing.Mount)

- `hinoki.routing.Route(...)`: Declared contract of one action, before it is bound to an application. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.routing.Route)

- `hinoki.project.manifest_routes(start: 'Path | None' = None) -> 'list[Includable]'`: The routes ``[project] routes`` declares in ``hinoki.toml`` (or ``[]`` when omitted). [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.project.manifest_routes)

- `hinoki.project.manifest_events(start: 'Path | None' = None) -> 'list[Handlers]'`: The ``Handlers`` each app of ``hinoki.toml`` declares, in dependency order, for ``Hinoki(events=manifest_events())``. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.project.manifest_events)

- `hinoki.project.manifest_middleware(start: 'Path | None' = None) -> 'list[Middleware]'`: The ``[project] middleware`` list of ``hinoki.toml``, flattened in listed order (the first listed runs outermost), for…. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.project.manifest_middleware)

- `hinoki.project.manifest_rules(start: 'Path | None' = None) -> 'RuleSet'`: The ``[project] rules`` list of ``hinoki.toml`` merged, in listed order, into one ``RuleSet`` for ``Hinoki(rules=manifest_rules())``. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.project.manifest_rules)

`Hinoki()` loads the routes, events, middleware and rules of `hinoki.toml` by
itself; call these functions only to pass the same lists by hand.
For model-backed route generation, see [Generate CRUD routes](https://hinoki.caspel.dev/dev/crud.md) and
the [CRUD API reference](https://hinoki.caspel.dev/dev/reference/crud.md).

## HTTP method decorators

Import the namespace and decorate actions: `from hinoki import http`, then `@http.get(...)`. The names are not available at the top level of `hinoki`.

- `hinoki.http.get(...)`: Declare a GET action. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.http.get)

- `hinoki.http.post(...)`: Declare a POST action. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.http.post)

- `hinoki.http.put(...)`: Declare a PUT action. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.http.put)

- `hinoki.http.patch(...)`: Declare a PATCH action. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.http.patch)

- `hinoki.http.delete(...)`: Declare a DELETE action. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.http.delete)

- `hinoki.http.query(...)`: Declare a QUERY action: a safe, idempotent request that carries a body (a search too large or too structured for a query string). [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.http.query)

## Request, response and errors

The types are available from `hinoki.http`, from `hinoki`, or from their modules `hinoki.http.request` and `hinoki.http.response`.

- `hinoki.http.request.Request(...)`: One HTTP request. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.http.request.Request)

- `hinoki.http.response.Response(status: 'int' = 200, body: 'bytes | str' = b'', headers: 'ResponseHeaders' = (), content_type: 'str | None' = None) -> 'None'`: Status, headers and body chosen by the handler. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.http.response.Response)

- `hinoki.http.response.set_cookie(...)`: A ``("set-cookie", ...)`` header for ``Response(headers=[...])``. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.http.response.set_cookie)

- `hinoki.http.Stream(body: 'Source', *, status: 'int' = 200, headers: 'ResponseHeaders' = (), content_type: 'str | None' = None, length: 'int | None' = None) -> 'None'`: A body of ``bytes`` or ``str`` pieces (a ``str`` is sent as UTF-8). [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.http.Stream)

- `hinoki.http.EventStream(...)`: Server-sent events (``text/event-stream``) from an iterable of ``Event`` or ``str`` (a ``str`` is the ``data`` of one event). [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.http.EventStream)

- `hinoki.http.Event(data: 'object' = None, *, event: 'str | None' = None, id: 'str | None' = None, retry: 'int | None' = None, comment: 'str | None' = None) -> 'None'`: One server-sent event. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.http.Event)

- `hinoki.http.File(...)`: A file sent by the server: ``File("reports/q3.pdf", root=REPORTS)``. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.http.File)

- `hinoki.access.Access(auth: 'BaseAuth[Any]', permissions: 'Iterable[str]' = ()) -> 'None'`: Which provider authenticates an action and which permissions it requires. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.access.Access)

- `hinoki.errors.APIError(...)`: An error with a public HTTP representation. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.errors.APIError)

- `hinoki.errors.BadRequest(...)`: An error with a public HTTP representation. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.errors.BadRequest)

- `hinoki.errors.Unauthorized(...)`: An error with a public HTTP representation. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.errors.Unauthorized)

- `hinoki.errors.Forbidden(...)`: An error with a public HTTP representation. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.errors.Forbidden)

- `hinoki.errors.NotFound(...)`: An error with a public HTTP representation. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.errors.NotFound)

- `hinoki.errors.Conflict(...)`: An error with a public HTTP representation. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.errors.Conflict)

- `hinoki.errors.TooManyRequests(retry_after: 'int', message: 'str | None' = None, *, code: 'str | None' = None) -> 'None'`: The one error a ``rate_limit`` policy may raise to refuse a request. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.errors.TooManyRequests)

- `hinoki.errors.UniqueConflict(...)`: A unique value is already in use: the answer to a ``UniqueViolation``. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.errors.UniqueConflict)

- `hinoki.errors.ObjectNotFound`: A query that must return one row returned none. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.errors.ObjectNotFound)

- `hinoki.errors.ErrorRegistry() -> 'None'`: Maps exception classes to handlers; the most specific class wins. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.errors.ErrorRegistry)

## Middleware

A project-wide middleware subclasses `Middleware`; see the [middleware
guide](https://hinoki.caspel.dev/dev/middleware.md). List it in `middleware` of `[project]` in `hinoki.toml`
or pass `middleware=[...]` to `Hinoki`.

- `hinoki.http.Middleware()`: Base class for a middleware; any object with this ``async`` call works. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.http.Middleware)

- `hinoki.http.MiddlewareRequest(...)`: The head of the request: no body, nothing to change. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.http.MiddlewareRequest)

- `hinoki.http.Next`. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.http.Next)

- `hinoki.http.cors.Cors(...)`: The browser origins that may read this action's answers. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.http.cors.Cors)

## WebSockets

The [WebSockets guide](https://hinoki.caspel.dev/dev/websockets.md) explains the handler, access policies, origins and limits.

- `hinoki.http.websocket(...)`: Declare a WebSocket action on ``path`` (``/rooms/{room}``), on a function or on a method of an ``API`` class (whose prefix it then takes). [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.http.websocket)

- `hinoki.http.WebSocket(transport: 'Transport', *, path: 'str', subprotocol: 'str | None' = None, message: 'type[Schema] | None' = None, reply: 'type[Schema] | None' = None) -> 'None'`: One open WebSocket, as the handler sees it. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.http.WebSocket)

- `hinoki.http.WebSocketClosed(code: 'int' = 1006, reason: 'str' = '') -> 'None'`: The connection is closed: the peer closed it, the server did (shutdown, idle timeout, a limit), or the handler did. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.http.WebSocketClosed)

- `hinoki.http.CloseCode(*values)`: The status codes a handler is likely to send (RFC 6455, section 7.4). [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.http.CloseCode)

## Rate limits

- `hinoki.ratelimit.RateLimitPolicy(*args, **kwargs)`: Anything with a ``check`` method, ``def`` or ``async def``. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.ratelimit.RateLimitPolicy)

- `hinoki.ratelimit.RateLimitContext(method: 'str', route: 'str', client: 'str | None', caller: 'Caller[Any] | None', lookup: 'Callable[[type[Any]], Any]') -> None`: What a policy may decide from: no headers, credentials, body or query. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.ratelimit.RateLimitContext)

- `hinoki.ratelimit.RateLimit(...)`: At most ``limit`` requests per ``window`` seconds for one key, counted in one shared table (or in memory, see ``storage``). [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.ratelimit.RateLimit)

- `hinoki.ratelimit.DatabaseLimits(table: 'str' = 'hinoki_rate_limits', max_rows: 'int' = 1000000, timeout: 'float' = 0.5, sweep_every: 'int' = 1000) -> None`: Buckets in a table of the worker's ``Database``, shared by every worker. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.ratelimit.DatabaseLimits)

- `hinoki.ratelimit.MemoryLimits(max_rows: 'int' = 100000, sweep_every: 'int' = 1000) -> None`: Buckets in the memory of ONE worker: not shared, reset on restart. [reference](https://hinoki.caspel.dev/dev/reference/web/#hinoki.ratelimit.MemoryLimits)
