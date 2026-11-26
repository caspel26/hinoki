---
status: implemented
related:
  - serving.md
  - request-lifecycle.md
  - rust-crud-orientation.md
---

# Kiso, Hinoki's Rust server

Kiso is the HTTP server inside Hinoki's native extension, written in Rust on
hyper, tokio and rustls. It is what `hinoki dev` and `hinoki serve` run unless
you choose Granian, and it can answer many requests without entering Python.

```sh
hinoki serve --workers 4
```

```python
# notes/settings.py
from hinoki import settings


class ProjectSettings(
    settings.AppSettings, prefix="NOTES_", dotenv=".env", toml="settings.toml"
):
    pass
```

```toml
# settings.toml
[server]
server = "kiso"
workers = 4
```

`--server kiso` selects it explicitly when a project defaults to Granian. Choose
Granian instead when you need an ASGI wrapper around the application
([Serve an application](serving.md#choose-a-server)).

## Request-head fuzzing

The fuzz workflow checks Kiso's HTTP/1 request-line and header
handling with two focused targets. `head_framing` checks chunk-boundary
invariance, the hard head-size bound, parser safety and that declared-length
bodies stay opaque. `request_head_limits` checks the `httparse` header-slot
bound, header-byte accounting, query-limit precedence and exact limit
boundaries. These targets run alongside the host, redirect, declared-length
and WebSocket fuzzers in CI; locally they can be run with
`cargo +nightly fuzz run head_framing` and
`cargo +nightly fuzz run request_head_limits`.

## What is native and what still runs in Python

The listener, HTTP/1.1 parsing, connection and header limits, body framing and its limits and timeouts, route matching, the request ID, and the answers that need no application code run on the native runtime without taking the Python interpreter lock. Without host, CORS or HTTPS-redirect policies, a request the native layer can answer by itself is answered there: unknown paths and methods (`404`, `405` with `Allow`), oversized request lines or headers, `503` while the worker is not ready or the in-flight limit is reached, and malformed requests. When one of those policies is active, the Rust classifier checks it first and then answers unmatched requests itself, with the same CORS headers as the Python pipeline. The response carries the same security headers, request ID, CORS and HSTS policy as one produced by the application.

The lifespan, handwritten handlers and their database calls still use Python. The OpenAPI document and the documentation pages and assets are built once at startup by the same Python code and answered by the Rust classifier for `GET` and `HEAD` (other methods continue to route lookup), with the same status, headers, request ID, CORS and security headers; they cross into Python only when a request target is not plain ASCII. Host allow-list checks, CORS preflight answers and HTTPS redirects run in the Rust classifier before docs and route lookup, with the same response order and headers as the Python pipeline. For matched routes that continue through the Python pipeline, Kiso also percent-decodes the captured path values and passes them to Python; an invalid UTF-8 escape is carried as an error marker and reported at the same point where the pipeline previously decoded it. Python still applies `str`, `int`, UUID, bool and custom converters, and owns authentication, hooks, body/query binding, handlers, error translation and response assembly.

For an eligible public `GET`, `DELETE`, `POST`, `PUT`, `PATCH` or `QUERY` function, Kiso skips Python route matching and request binding. It accepts parameters from the path and query string annotated `str`, `int`, `float`, `bool` or `UUID` with the built-in converter, and query parameters whose default has exactly that type. Rust decodes provided values, converts them with the same rules as the Python converters (an invalid value gives the same `422`), validates the full query, negotiates `Accept`, and prepares the handler arguments. An absent optional query uses its declared default, including when a wrapper observes the keyword arguments; an empty value is still an explicitly supplied empty string. Missing required or repeated query parameters use the same validation response as the Python pipeline. Error translation and `Response` creation remain in Python; JSON encoding uses Hinoki's Rust encoder, and Rust finalizes the response headers and suppresses the body for `HEAD`. Those final headers include the CORS headers for the request's `Origin`, on successful and error answers alike, exactly as the Python pipeline adds them; an answer that sets its own `Access-Control-Allow-Origin` keeps its CORS headers. An async handler runs in its ordinary asyncio task, unless it qualifies for [the native handler lane](#the-native-handler-lane); a sync handler uses the bounded executor. If a sync handler returns an awaitable, Hinoki awaits it on the same request task after the executor call completes. An integer longer than 18 digits is left to the Python pipeline, which applies Python's own integer limits. A body method is eligible with exactly one `Schema` body parameter and only built-in parsers (`JSONParser`, `FormParser`, `TextParser`, `BytesParser`, exact types without overrides; a custom parser or a `JSONParser.prepare` override uses the Python pipeline). The route's parser is chosen from the `Content-Type` after the body is read, so an unlisted type is the same `415`. Its body is read from the native connection only when the handler's turn comes, after content negotiation, with the same limits, and is then decoded and validated by the Rust decoder and validator. The failures keep the order and the answers of the Python pipeline: a declared length above the limit or a body read failure first, then a malformed query string, then the content type (`415`) and malformed JSON (`400`), then one `422` listing every path, query and body problem in declaration order. A refusal before the body is read closes the connection, as on the Python pipeline. The automatic path does not accept several body parameters, file or `Request` parameters, custom parsers, multipart uploads, defaults on path parameters, custom response headers or custom renderers. The built-in `JSONRenderer`, `HTMLRenderer`, `TextRenderer` and `XMLRenderer`, alone or several on one route, are eligible: Rust negotiates `Accept` (including `406`) and Python renders with the chosen one, exactly as on the Python pipeline. A subclass of a built-in renderer or one with extra attributes is custom. A route with an `access` policy stays eligible, with at most one `Caller` principal parameter: Python authenticates the request before anything else, so the order of the answers (`400`, `invalid_query`, `401`/`403`) matches the Python pipeline, and the body is still read only after authentication succeeds. When that access policy uses an unmodified [`ClaimsJWTAuth`](authentication.md#claims-only-tokens-on-the-native-lane), Rust also answers the refusals itself (`401` with the challenge, or `403`) without crossing into Python, and hands the admitted caller to the handler. A route with a `rate_limit` policy is eligible too: Python asks the policy, with the same client address as the Python pipeline (the forwarded client when the TCP peer is a trusted proxy, otherwise the peer) and the authenticated caller, right after authentication and before negotiation, the body and validation, so a refused client gets the same `429` or `503` without the server reading its body. A route with `atomic=True` stays eligible: the handler runs inside the same transaction as on the Python pipeline, the commit follows the encoding of the answer, and any error or timeout rolls back before it is translated. Host restrictions, CORS and HTTPS redirects are checked natively and do not make an otherwise eligible route fall back. Every other route uses the full Python request pipeline for binding, validation, error translation and response assembly. The request body is read natively only when that pipeline asks for it, after authentication, so an unauthenticated client cannot make the server buffer a body. Handlers themselves remain Python code; this is not an application written in Rust, and no performance figure is claimed here. A `Query[Schema]` parameter of a `GET` or `DELETE` is eligible too (Python converts and validates the query string, reporting errors in the pipeline order; it does not combine with a body). A `Form[Schema]` or `XML[Schema]` body is eligible too: the text is converted and validated as described in [Form bodies](endpoints.md#form-bodies) and [XML bodies](endpoints.md#xml-bodies).

## The native handler lane

An `async def` handler that only talks to the database through the ORM can run without an asyncio task: Kiso drives its coroutine itself, runs each database wait on the native runtime and sends the answer from there. Hinoki decides this once per route at worker startup, by reading the handler's own source. A route takes the lane when:

* it is a `GET`, `DELETE`, `POST`, `PUT`, `PATCH` or `QUERY` that is eligible for the native path above (a body method takes one `Schema` body and built-in parsers);
* the function is a plain `async def`, not wrapped by a decorator;
* every `await` in it is an ORM call on a `Model` class:
    * a read: `Model.aget(...)`, `Model.aget_or_none(...)`, `Model.aget_as(...)`, `Model.aget_or_none_as(...)`, or `Model.filter(...)` / `Model.all()` with `filter`, `select_related`, `prefetch_related`, `order_by`, `limit` and `offset`, ending in `aget`, `aget_as`, `aget_or_none_as`, `afirst`, `acount`, `aexists`, `aproject`, or `apaginate_as`, or awaited directly;
    * a write: `acreate`, `aupdate`, `aupdate_where`, `adelete`, `abulk_create`, `abulk_update`, `abulk_delete`, or `aupdate` / `adelete` ending a `filter(...)` chain. A write counts only on a model that overrides none of the write hooks (`before_save`, `before_create`, `after_create`, `after_save`) or the methods the write calls (`insert`, `asave`, `from_schema` and the like); `asave`, `aupdate_from` and `aupdate_within` on an instance stay on asyncio.

These keep a route in an asyncio task: `async with`, `async for`, `yield`, async comprehensions, local names that shadow a model, and any other `await` (`asyncio.sleep`, an HTTP client, another coroutine).

* **Guards and bodies.** A guarded route (`access`, `rate_limit`, `Query[Schema]` or several renderers) is served the same way, and so is a body method. The built-in `RateLimit` counts in one SQL statement on the native runtime without an asyncio task, so a route that uses it keeps its lane. Guards run first inside the driven coroutine, in the order of the asyncio path (credentials, then the rate limit, then the body and the request values), and the body is read on the native runtime.
* **Class APIs.** A handler of a class API takes the lane when the class keeps the stock constructor (no `__init__` or `__new__` of its own in the class or its bases) and adds no `response_headers`. The instance is built per request, as on the asyncio path; a class that builds itself with application code stays on asyncio. The constructor is read once at startup.
* **Transactions.** An `atomic=True` route runs its handler inside a transaction opened, committed or rolled back on the native runtime, and an error is translated only after the rollback, as on the asyncio path.
* **Invalid requests.** A request Rust already finds invalid (a bad path encoding, an invalid path or query parameter, an unacceptable `Accept`) stays on the asyncio path.
* **Moving to asyncio.** A provider or policy that awaits something only asyncio can run (a custom provider calling an HTTP service, a custom rate limit policy whose store awaits on asyncio) moves the request to an asyncio task at that point, once, with what is left of the timeout. A transaction already open goes with it, so later savepoints still nest and the rollback is unchanged. The same happens if a write hook is added to a model after startup and awaits asyncio.

`hinoki routes --lanes` shows the choice, and `hinoki.runtime` logs the reason for every route at debug level.

The behaviour is the same on both paths: status, headers, body, error translation, the request ID and the `request_timeout` (a `504`). An error from a driven handler, and a failure of a generated CRUD action, is translated in place when no error handler you registered can match it; an exception one of your handlers matches is translated in an asyncio task, because the handler may await. Each request has its own context, so `request_id` and the database are the ones of that request. The handler's steps after its first run on the event-loop thread, so a task it starts still finds the loop. If a driven handler awaits something only asyncio can run (a database backend without native waits), it is moved to an asyncio task at that point and continues with what is left of the request timeout. At shutdown, running handlers are cancelled like request tasks.

What it assumes: model classes and their ORM methods are not replaced after startup. Set `server.native_dispatch = false` to keep every handler in an asyncio task. No performance figure is claimed here.

`hinoki routes --lanes` shows, per route, whether Kiso runs it natively and, if
not, why ([Inspect an application](inspect.md#routes)).

## Generated CRUD

Generated CRUD plans can also execute public `create` (`POST`), `update` (`PATCH`) and `delete` actions in Rust. These routes use the native path only when their model and schemas are supported and they have no assignment rule or custom CRUD hook (an `owner` is allowed only with `identity_key = caller_id`, and a `scope` with `RowScope.by_caller` or `RowScope.by_claim` on a text non-key column outside the update schema, on any supported database), and their access policy, if any, uses an unmodified [`ClaimsJWTAuth`](authentication.md#claims-only-tokens-on-the-native-lane). A missing, empty or non-string required claim on a scoped route answers `403 Forbidden` before opening a transaction or querying. Rust reads and validates the bounded request body, runs the database transaction, fetches the written row where the response requires it, and encodes the response before commit. Writes continue to completion if the client disconnects. A unique-constraint violation returns `409 Conflict` by default; a registered application error handler takes precedence. Routes that are ineligible or become stale use the ordinary Python CRUD pipeline. A model with a handler for `Created`, `Updated` or `Deleted` ([model events](events.md#model-events)) is ineligible for the matching action only, reason `model_event_handlers`. Plans are selected automatically at worker startup and checked once per second; a changed plan is disarmed until the worker restarts. This path adds no public setting.

These model features are supported on the native CRUD lane:

* `SoftDeleteModel` (or `@db.soft_delete()`): a `delete` stamps the soft-delete column (and `updated_at`) with an `UPDATE`, and every native read sees live rows only, with the same SQL as the Python lane.
* UUIDv4 or UUIDv7 keys from `uuid_pk()`, and `urlsafe_token()` columns that no schema writes, all generated in Rust.
* `Decimal` and `date` columns. A stored `Decimal` that Rust cannot render exactly as Python would, such as one Python writes in exponent form, is handed back to Python for that request.

`Enum` columns, `RowScope.by(...)` and `CursorPagination` lists stay on the Python lane. Run with the `hinoki.server` logger at debug level to see, at startup, why each generated CRUD route stays on the Python lane.

The measured phase 1 path is documented in the [native CRUD orientation run](rust-crud-orientation.md). It is a single-host, workload-specific measurement and does not establish general or production performance.

Routes with a [many-to-many ids field](crud.md#many-to-many-fields) take the same path when each relation is scoped with `all_rows(Target)`, `own_rows(Target, column)`, `RowScope.by_caller(Target, column)` or `RowScope.by_claim(Target, column, claim)` and no `Linked`/`Unlinked` handler is registered. Rust reads the ids of a page with one extra `SELECT` on the bridge table, and on create and update it parses the set, locks the owner row (with its row scope and owner policy), checks the targets against the scope and writes the bridge in the row's transaction; the response is encoded before commit, with the ids read inside it. Python is entered only for a key Rust cannot read, which the Python lane then answers. `RowScope.by(...)`, a scope callable, a `link_queryset` override or a hook keeps the route on the Python lane.

## Limits

* HTTP/1.1, plus HTTP/2 over TLS when `server.http2` is on (see below). WebSocket upgrades are accepted for the actions declared with `@http.websocket` (see [WebSockets](websockets.md)); any other `Upgrade` header is ignored and the request is answered as plain HTTP, and no other protocol is ever switched to. There is no cleartext HTTP/2 (neither prior knowledge nor `Upgrade: h2c`).
* A request line plus headers larger than `execution.max_header_bytes`, or a query longer than `execution.max_query_bytes`, is refused (`431`, `414`). A body larger than `execution.max_body_bytes` is refused with `413`, by `Content-Length` before any byte is read or, for chunked bodies, as soon as the total passes the limit. A client that sends nothing for `body_read_timeout` seconds (10 by default, never more than half of `execution.request_timeout`) gets `408`, and so does one that trickles data: the whole body must arrive within six times `body_read_timeout`.
* A client that does not finish its request headers within `header_read_timeout` seconds (10 by default) is disconnected; the same timer closes idle keep-alive connections.
* TLS is optional: set `server.tls_cert_file` and `server.tls_key_file` (PEM; the key may be PKCS#8, PKCS#1 or SEC1) and Kiso speaks HTTPS with rustls (TLS 1.2 and 1.3, the library's default cipher suites, no client certificates, ALPN `http/1.1`, or `h2` then `http/1.1` with `server.http2`). The files are read once at start, so a renewed certificate needs a restart (a rolling restart of the workers is enough). A plaintext client on a TLS port gets a TLS alert, never an answer, and the handshake must finish within `header_read_timeout`. Requests over TLS have the scheme `https`, so the HSTS policy applies. `hinoki serve` warns when the key is readable by every local user. Granian takes the same two settings (the framework asks it for TLS 1.2 and up and HTTP/1.1 only, as Kiso does, unless `server.http2` is on). A trusted proxy's `X-Forwarded-Proto` overrides the scheme, `http` included, on both servers. The handshake and the request head each have their own `header_read_timeout`, so a slow client can hold a connection slot for up to twice that time. Behind a proxy that terminates TLS, leave them unset and list the proxy in `trusted_proxies` instead.
* HTTP/2 is opt-in: set `server.http2 = true` together with the TLS files (it is a settings error without them, on both servers). The server then offers ALPN `h2` and `http/1.1` and serves each connection with the protocol it negotiated; a client that offers only `http/1.1`, or no ALPN, still gets HTTP/1.1. Requests reach the application unchanged, with `http_version` `2` in the ASGI scope; the `:authority` pseudo-header becomes the `Host` and wins over a `host` header that differs. Limits are fixed, not settings: 32 concurrent streams per connection (the 33rd is refused), a 64 KiB stream window, header lists bounded by `execution.max_header_bytes` and at most 100 header lines per request (`cookie` fields count once; more is answered `431`), and the h2 library's rapid-reset protections. A connection counts as one slot of `max_connections`, but every stream in flight uses backend capacity (the in-flight limit and the queue) like a request of its own, so size those limits for connections times 32. The server also drops a connection on which more than 100 streams end before their response completed (client resets) within a fixed 10-second window; it is closed without a `GOAWAY`. The count is per connection, not per client: a client can pass about twice the bound across a window boundary, and a new connection starts from zero. A connection with no open stream for `header_read_timeout` is closed gracefully (`GOAWAY`; a stream opened in the race with the `GOAWAY` is accepted and then cut when the shutdown timeout ends). A response that sends no data for `write_timeout` seconds (a client that keeps its flow-control window at zero, or stopped reading) also gets the connection a `GOAWAY`: hyper cannot reset one stream from outside, so the other streams have the shutdown timeout to finish and the stalled one is cut with the connection. Progress is tracked per response, so opening new streams does not hide a stalled one. Keep-alive pings every 30 seconds drop a peer that stopped answering. Set `write_timeout` above 32 x 16 KiB divided by the slowest client bandwidth you expect to serve: HTTP/2 shares the window between streams unevenly, so on a slow link each of 32 concurrent downloads can wait that long between two chunks and be taken for stalled while bytes still flow. A request with several `host` headers and no `:authority` is refused with `400`. A client that trickles the head of one stream while others are open is not bounded by `header_read_timeout`. On stop the server sends `GOAWAY`, lets streams in flight finish, and aborts what is left after the shutdown timeout. Not provided: cleartext HTTP/2 (h2c), server push, and WebSocket or `CONNECT` over HTTP/2. Granian with `server.http2` negotiates HTTP/2 itself (`http` mode `auto`), under its own limits.
* A client that stops reading a response for `write_timeout` seconds (30 by default) is disconnected; any progress restarts the timer.
* `max_connections` (10000 by default) bounds open connections. Requests waiting for the application are bounded by an in-flight limit and a queue; beyond them the answer is `503`.
* Requests with ambiguous framing (for example `Content-Length` together with `Transfer-Encoding`, repeated lengths, or a bare line feed in the head) are refused with `400` and the connection is closed.
* On start, Kiso names itself on stderr before the `listening on ...` line (which stays on stdout, where tools wait for it): its version, the mode (`serve` or `dev`, which reloads on file changes), the worker count, the scheme, whether the access log is on and whether the native handler lane is on. In a terminal (and with `NO_COLOR` unset) it is the same banner `hinoki new` draws: a branch (`╭─`, `│`, `╰─`) titled **Kiso** (no image, on any terminal) with the tagline and the version, then the facts as aligned `mode`, `workers`, `scheme`, `access log` and `native lane` rows, wrapped to fit the terminal; otherwise it is one plain line, for example `Kiso 0.0.1 (Hinoki's Rust server): serve, 4 workers, http, access log off, native lane on`.
* The access log is one line per answered request, written to stderr by Kiso: `127.0.0.1:52341 - "GET /items?limit=5 HTTP/1.1" 200 OK 3.2ms`. The time is how long the server took to decide the answer, not including writing it to the client, and the line is printed for every answer, including the ones the server gives itself (`404`, `429`, `503`). When stderr is a terminal (and `NO_COLOR` is unset) the line is coloured in the style of a development console: `INFO:` in green, the request in bold, the status by class (2xx green, 3xx cyan, 4xx yellow, 5xx red) and the time dimmed. Control characters in the target are shown as `\xNN`. Other formats and filters:
    * `server.access_log_format = "json"` writes one JSON object per line instead, for a log pipeline: `{"time":"2026-11-19T19:56:25.881Z","peer":"127.0.0.1:52341","method":"GET","path":"/items","query":"limit=5","http_version":"1.1","status":200,"duration_ms":3.2,"lane":"python"}`. `time` is RFC 3339 UTC. `lane` says where the request was first routed: `early` for an answer from the transport or the classifier, `native` for a [generated CRUD plan](#generated-crud) that Rust executes, and `python` for every request handed to the Python side, including handlers on the [native handler lane](#the-native-handler-lane), so it can differ from what `hinoki routes --lanes` shows. `route` is the pattern that matched, e.g. `/items/{id}`, so requests can be grouped without parsing paths (absent when no route matched). Quotes and control characters are escaped.
    * `server.access_log_format = "logger"` hands each request to the Python `hinoki.access` logger instead of writing to stderr, so your own handlers, formatters and filters (see [your own loggers](settings.md#your-own-loggers)) decide where it goes; each record carries the JSON fields as `record.access`. Kiso queues up to 4096 records and a logging thread passes them to Python in batches, taking the GIL once per batch; when the logger falls behind, new records are dropped rather than slowing requests.
    * Setting a format turns the log on, in `serve` too, unless `access_log` is `false`.
    * `access_log_skip = ["/health", "/static/*"]` leaves noisy paths out and `access_log_slow_ms = 100` keeps only slow answers; failures (status 400 and up) are always logged.
* A client that disconnects does not cancel its running handler; the handler finishes and its answer is discarded.
* A WSGI callable (`--interface wsgi`) runs in Python threads behind this listener, not on the native lane: see [WSGI on Kiso](serving.md#wsgi-on-kiso) for what applies to it.
* An answer Kiso cannot send as given (an informational status, a header name or value HTTP does not allow, an unreadable `content-length`) becomes a bare `500`, and the reason is written to stderr, for example `kiso: error: the response cannot be sent (invalid header "x-evil"); answering 500 instead`. The access log records the `500`. A streamed body that is shorter or longer than its `content-length` is cut off with an error line on stderr and the connection is closed (HTTP/1.1) or the stream reset (HTTP/2), so the client never mistakes it for a complete answer. The access log keeps the status of the head here, because it is written when the head is decided.

## Streamed responses

An action that returns `Stream`, `EventStream` or `File` is never served by the native handler lane (`lane_reason` `streamed response`): its handler and its iterator run in Python, and each piece crosses to Rust as a Python `bytes`. After that the server frames the body (`transfer-encoding: chunked` on HTTP/1.1), keeps a bounded queue of 8 pieces so a slow client slows the iterator down, checks a declared `content-length`, applies `execution.stream_idle_timeout` to a `Stream`, sends a keep-alive comment to an event stream every `execution.sse_keepalive` seconds, and ends an event stream normally on shutdown. A `File` is opened and sent by the server, and its bytes never enter Python.

A client that leaves is noticed when the server next writes to it, so a stream that keeps producing is closed at once, while a silent one is closed at its next keep-alive or idle timeout. A body that fails after the head is cut instead of ending as complete, and the access log keeps the status of the head. See [stream a response](streaming.md).

## Response compression

With `compression.enabled` Kiso compresses eligible bodies with brotli or gzip after the application has answered, on HTTP/1.1 and HTTP/2, for whole answers, streams, event streams and files. It negotiates `accept-encoding`, adds `Vary`, skips small or non-text bodies and answers with `cache-control: no-transform`, flushes a stream whenever its producer pauses, and sends an answer uncompressed when `compression.max_concurrent` compressions are already running. It is a Kiso feature: Granian does not compress. See [compress responses](compression.md).

## Workers, signals and restarts

With one worker and `server.sighup = "stop"` (or where the system has no `SIGHUP`, as on Windows) `hinoki serve` runs the server in its own process. With several workers, with the default `sighup` (one extra supervisor process even for one worker) and always under `hinoki dev` it starts a supervisor that starts each worker as a fresh process that imports the target and builds the application. Databases and pools open inside each worker's lifespan, never in the supervisor.

* **Listening sockets and `SO_REUSEPORT`.** On Linux, when `hinoki serve` runs more than one worker on a non-zero port (and `server.reuse_port` is not `false`), the supervisor probes `host:port` to validate it and then closes its probe socket before spawning workers; each worker opens its own `SO_REUSEPORT` listening socket (honouring `backlog`) after its lifespan startup succeeds, so the Linux kernel distributes incoming connections across per-worker accept queues. On macOS and other non-Linux platforms (where `SO_REUSEPORT` does not load-balance across sockets), with one worker, with `port = 0` (ephemeral port), under `hinoki dev`, or when `server.reuse_port = false`, the supervisor opens one listening socket (honouring `backlog`) and passes the descriptor down to every worker; connections are then spread by the operating system's accept on that shared socket.

* `SIGTERM` or `SIGINT` (Ctrl-C) stops accepting, lets running requests finish for up to `lifecycle.shutdown_timeout`, runs the lifespan shutdown and exits. `SIGHUP` does the same when `server.sighup` is `"stop"` and [reloads](#reload-on-sighup) otherwise. A worker still running five seconds after that is killed. A second signal kills the workers immediately. Once the stop begins the supervisor closes its own copy of the socket, so a client that connects while the workers drain is refused. `--workers` must be 1 to 256 (`0` uses `server.workers`).
* A worker that exits on its own, whether it failed to start or crashed, is not restarted: the supervisor stops the other workers and exits with a non-zero status. Restart policy belongs to the process manager (systemd, a container runtime) that runs `hinoki serve`.
* The listening socket is opened before the lifespan startup of a supervised worker finishes, so connections wait in the backlog until a worker accepts them. With one worker the socket is opened after a successful startup.
* If the supervisor is killed without a chance to clean up, its workers notice within a second of finishing their startup and stop. The workers' copy of the listening socket is not inherited by processes a handler starts.
* A socket that cannot be opened (address in use, a bad port or host) is reported as `error: cannot listen on HOST:PORT: ...` with exit status 2, with one worker or several.

## Reload on `SIGHUP`

`kill -HUP <supervisor pid>` (or `ExecReload=/bin/kill -HUP $MAINPID` in a systemd unit) replaces the workers of a running `hinoki serve` with new ones, without closing the listening socket. The supervisor does this one worker at a time:

1. It starts a new worker on the same socket (or, with per-worker `SO_REUSEPORT` on Linux, with the same `host:port`, so the new worker binds its own `SO_REUSEPORT` socket only after its lifespan startup succeeds). The new process imports the target and reads the settings, so changed code, `.env` and TOML files, TLS certificates, and CORS, host and security policies take effect.
2. It waits until the new worker has run its lifespan startup and serves. The wait is `lifecycle.startup_timeout` plus five seconds.
3. It sends `SIGTERM` to the old worker, which stops accepting (closing its `SO_REUSEPORT` socket immediately on Linux so new connections go only to the active workers), lets the requests it has finish (up to `lifecycle.shutdown_timeout`), runs its lifespan shutdown and exits. It is killed if it is still running five seconds after that.
4. It moves to the next worker. At most one extra worker runs at a time, so the database connections of an application are at most `(workers + 1) × pool_max` while a reload runs.

```text
reloading: SIGHUP (generation 2)
reloaded 4 workers (generation 2)
```

The supervisor's own settings are not reloaded: `host`, `port`, `backlog`, `workers`, `server`, `interface` and `metrics.listen` need a restart, as do the environment variables of the supervisor process. Migrations are never run by a reload: run `hinoki migrations apply` before sending the signal.

* A new worker that exits, fails its lifespan startup or is not ready in time is killed, and the reload stops with `error: reload stopped at worker K of N; the other workers keep running`. The workers not yet replaced keep serving the old code (workers already replaced stay new); another `SIGHUP` retries them all. The supervisor does not exit.
* A `SIGHUP` that arrives during a reload is remembered once and handled when it ends. `SIGTERM` or `SIGINT` during a reload stops everything, the new and the old workers, as usual.
* A worker that crashes outside a reload still stops the supervisor, as above.
* `server.sighup = "stop"` keeps the earlier behavior (`SIGHUP` stops the server). `hinoki dev` always reloads on file changes and `SIGHUP` stops it. Windows has no `SIGHUP`, so there the setting has no effect and the server runs as for `"stop"`. Granian refuses `server.sighup`.
* A request that a client has already sent to the old worker when it is told to stop is answered, not refused with a `503`: the worker reports not ready only after its connections have drained.
* The reload is orchestrated by the supervisor, which is Python; the drain of the old worker and the TLS loading of the new one are Rust. With metrics, see [several workers](metrics.md#several-workers).

```python
# notes/settings.py
from hinoki import settings


class ProjectSettings(
    settings.AppSettings, prefix="NOTES_", dotenv=".env", toml="settings.toml"
):
    pass
```

```toml
# settings.toml: reload is on by default; "stop" turns it off
[server]
workers = 4
sighup = "reload"
```

```sh
hinoki serve notes.main:app &
kill -HUP "$(pgrep -f 'hinoki serve')"
```

## Reload with `hinoki dev`

The supervisor watches the project directory (`*.py`, `*.toml` and `.env` files; `.venv`, `.git` and caches are ignored) and, on a change, stops the worker gracefully and starts a new one on the same socket, so a client connecting during the restart waits instead of being refused. If the new code fails to import or start, the error is printed (a short `error: cannot load ...` line for a missing module or name, a traceback for other failures), `waiting for changes` follows, and the next edit starts the worker again; connections wait in the meantime. The host, port, `backlog` and the choice of server are read once when `hinoki dev` starts: change them and restart the command. The other `server` settings (limits, `trusted_proxies`, `runtime_threads`) are read again by every restarted worker. The watcher ignores `.venv` but not a virtual environment with another name, such as `venv/`: keep it outside the project directory or it triggers reloads. The first import of the target happens in the command itself, so an error there stops it with a message instead of waiting. `dev` needs `watchfiles` (the `hinoki-api[server]` extra) and uses one worker.

## Behind a reverse proxy

`server.trusted_proxies` works as described in [Hosts, CORS and HTTPS](web-security.md#reverse-proxies-and-request-ids): the native layer matches the TCP peer against it for the request ID and the HSTS and scheme policy of its own answers, and the request pipeline resolves the forwarded scheme and client address of the requests it handles from the same peer. The rate limit of a natively served route resolves its client the same way, so `RateLimitContext.client` is the same on both lanes. Points to know:

* Entries are IP addresses or CIDRs. An IPv6 address with a zone id such as `fe80::1%lo0` is refused when settings load, for either server (earlier versions loaded it and the ASGI path matched it; Kiso cannot take it).
* On a listener bound to `::` that also accepts IPv4 (the default on many systems, for example Linux with `bindv6only=0` and macOS), an IPv4 peer appears as `::ffff:a.b.c.d`. A plain IPv4 entry such as `10.0.0.5` or `10.0.0.0/8` then never matches, and the proxy is treated as untrusted. That fails closed, and the ASGI path behaves the same. Either bind an IPv4 host (`--host 0.0.0.0`) or list the mapped form, for example `::ffff:10.0.0.0/104`.
* A request ID that a trusted proxy sent and Hinoki refused is logged as `ignored invalid request ID from trusted proxy`; for requests answered natively the warning is aggregated once a second with a count.

## Errors and edge cases

- A build whose native extension has no Kiso fails `--server kiso` with an error that says so.
- A socket that cannot be opened is reported as `error: cannot listen on HOST:PORT: ...` with exit status `2`.
- A worker that exits on its own is not restarted: restart policy belongs to your process manager.

## See also

- [Serve an application](serving.md)
- [Request lifecycle](request-lifecycle.md)
- [Native CRUD orientation run](rust-crud-orientation.md)
