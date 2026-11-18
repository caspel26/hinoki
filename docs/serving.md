---
status: implemented
related:
  - settings.md
  - cli.md
---

# Running an application

```sh
cd examples
uv run --project .. hinoki dev   blog.main:create_app     # development: reload on change
uv run --project .. hinoki serve blog.main:create_app     # production-style: no reload
```

`TARGET` is `module:name`, where `name` is an application (`Hinoki`) or a function that returns one (a factory: the usual choice, because it builds the app inside each worker). The factory builds declarations only; databases and pools open in the worker's lifespan.

With a declared app, settings load when the target module creates the `Hinoki` object. The database declared by the settings (or a configured `DatabaseModule`) opens that worker's pool during lifespan startup and closes it during shutdown. The pool is per worker; no database connection is opened by importing model or route modules. See [settings and database ownership](settings.md#8-database-connection-ownership).

Both commands run the app under Granian unless you choose the [Rust server](#the-rust-server) with `--server rust` or `server.server`. In the Hinoki checkout,
`uv sync --locked` installs the development environment, including Granian's
reload support. A project created by `hinoki new` declares the server extra in
its source dependency; run `uv sync` in that project. The extra includes
Granian's reload support (`watchfiles`), which `hinoki dev` needs. If Granian
was installed separately without `watchfiles`, `dev` reports the missing
dependency and `serve` still works. For this checkout, use its locked source
environment instead of installing `hinoki[server]` from the public package
index; see [installation](getting-started.md) for setup.

## Where host, port and workers come from

From the application's **`server` settings group** ([settings guide](settings.md)); flags override them:

| Setting | Variable (prefix `BLOG_`) | Default | Flag |
| --- | --- | --- | --- |
| `server.host` | `BLOG_SERVER__HOST` | `127.0.0.1` | `--host` |
| `server.port` | `BLOG_SERVER__PORT` | `8000` | `--port` |
| `server.workers` | `BLOG_SERVER__WORKERS` | `1` | `--workers` (serve) |
| `server.runtime_threads` | `BLOG_SERVER__RUNTIME_THREADS` | `1` | |
| `server.backlog` | `BLOG_SERVER__BACKLOG` | `1024` | |
| `server.access_log` | `BLOG_SERVER__ACCESS_LOG` | `false` | |
| `server.server` | `BLOG_SERVER__SERVER` | `granian` | `--server` |

`dev` always runs one worker with reload. The settings command shows what is in effect: `hinoki settings blog.settings:BlogSettings`.

## The Rust server

`hinoki serve --server rust` (or `server.server = "rust"`) runs the application on a second server that is part of Hinoki's native extension. Granian stays the default and `--server granian` always selects it; nothing changes for an app that does not opt in.

```sh
uv run --project .. hinoki serve blog.main:create_app --server rust
uv run --project .. hinoki serve blog.main:create_app --server rust --workers 4
uv run --project .. hinoki dev   blog.main:create_app --server rust
```

```python
from hinoki import settings


class BlogSettings(settings.AppSettings, prefix="BLOG_"):
    server: settings.ServerSettings = settings.ServerSettings(server="rust", workers=4)
```

### What is native and what still runs in Python

The listener, HTTP/1.1 parsing, connection and header limits, body framing and its limits and timeouts, route matching, the request ID, and the answers that need no application code run on the native runtime without taking the Python interpreter lock. Without host, CORS or HTTPS-redirect policies, a request the native layer can answer by itself is answered there: unknown paths and methods (`404`, `405` with `Allow`), oversized request lines or headers, `503` while the worker is not ready or the in-flight limit is reached, and malformed requests. When one of those policies is active, the Rust classifier still checks it first, then preserves Python lookup for unmatched requests. The response carries the same security headers, request ID, CORS and HSTS policy as one produced by the application.

The lifespan, documentation and OpenAPI pages, handwritten handlers and their database calls still use Python. Host allow-list checks, CORS preflight answers and HTTPS redirects run in the Rust classifier before docs and route lookup, with the same response order and headers as the Python pipeline. For matched routes that continue through the Python pipeline, the Rust server also percent-decodes the captured path values and passes them to Python; an invalid UTF-8 escape is carried as an error marker and reported at the same point where the pipeline previously decoded it. Python still applies `str`, `int`, UUID, bool and custom converters, and owns authentication, hooks, body/query binding, handlers, error translation and response assembly.

For an eligible public `GET`, `DELETE`, `POST`, `PUT`, `PATCH` or `QUERY` function, the Rust server skips Python route matching and request binding. It accepts parameters from the path and query string annotated `str`, `int`, `float`, `bool` or `UUID` with the built-in converter, and query parameters whose default has exactly that type. Rust decodes provided values, converts them with the same rules as the Python converters (an invalid value gives the same `422`), validates the full query, negotiates `Accept`, and prepares the handler arguments. An absent optional query uses its declared default, including when a wrapper observes the keyword arguments; an empty value is still an explicitly supplied empty string. Missing required or repeated query parameters use the same validation response as the Python pipeline. Error translation and `Response` creation remain in Python; JSON encoding uses Hinoki's Rust encoder, and Rust finalizes the response headers and suppresses the body for `HEAD`. Those final headers include the CORS headers for the request's `Origin`, on successful and error answers alike, exactly as the Python pipeline adds them; an answer that sets its own `Access-Control-Allow-Origin` keeps its CORS headers. An async handler runs in its ordinary asyncio task, while a sync handler uses the bounded executor. If a sync handler returns an awaitable, Hinoki awaits it on the same request task after the executor call completes. An integer longer than 18 digits is left to the Python pipeline, which applies Python's own integer limits. A body method is eligible with exactly one `Schema` body parameter and the default JSON parser. Its body is read from the native connection only when the handler's turn comes, after content negotiation, with the same limits, and is then decoded and validated by the Rust decoder and validator. The failures keep the order and the answers of the Python pipeline: a declared length above the limit or a body read failure first, then a malformed query string, then the content type (`415`) and malformed JSON (`400`), then one `422` listing every path, query and body problem in declaration order. A refusal before the body is read closes the connection, as on the Python pipeline. The automatic path does not accept several body parameters, file or `Request` parameters, custom parsers, defaults on path parameters, access policies, custom response headers or renderers. A route with `transaction=True` stays eligible: the handler runs inside the same transaction as on the Python pipeline, the commit follows the encoding of the answer, and any error or timeout rolls back before it is translated. Host restrictions, CORS and HTTPS redirects are checked natively and do not make an otherwise eligible route fall back. Every other route uses the full Python request pipeline for binding, access policies, validation, error translation and response assembly. The request body is read natively only when that pipeline asks for it, after authentication, so an unauthenticated client cannot make the server buffer a body. Handlers themselves remain Python code; this is not an application written in Rust, and no performance figure is claimed here.

### Generated CRUD

Generated CRUD plans can also execute public `create` (`POST`), `update` (`PATCH`) and `delete` actions in Rust. These routes use the native path only when their model and schemas are supported and they have no access policy, owner/assignment rule or custom CRUD hook. Rust reads and validates the bounded request body, runs the database transaction, fetches the written row where the response requires it, and encodes the response before commit. Writes continue to completion if the client disconnects. A unique-constraint violation returns `409 Conflict` by default; a registered application error handler takes precedence. Routes that are ineligible or become stale use the ordinary Python CRUD pipeline. Plans are selected automatically at worker startup and checked once per second; a changed plan is disarmed until the worker restarts. This path adds no public setting.

The measured phase 1 path is documented in the [native CRUD orientation run](rust-crud-orientation.md). It is a single-host, workload-specific measurement and does not establish general or production performance.

### Optional startup probe

`Hinoki(startup_url="/health")` reserves one public, fixed `GET`/`HEAD` endpoint and returns `{"status":"ok"}` after that worker's lifespan startup has completed. The setting is off by default. Its path must be a literal ASCII path; query strings, fragments, parameters, percent escapes and dot segments are rejected. It must not match an application route or an enabled OpenAPI/docs path. The endpoint is not an application route and does not appear in OpenAPI or generated CRUD/MCP tools.

This is a **startup probe**, not a general readiness or liveness check. A `200` says that startup completed (and, on the Rust server, its request wake reader was installed); it does not check database availability, downstream services, request capacity, or whether the Python event loop continues to make progress. It answers independently of the Python request queue when the Rust server can apply the response directly. Before readiness it returns `503 not_ready`; during shutdown readiness is cleared before graceful draining begins.

ASGI/Granian and WSGI answer it in the Python pipeline after host, CORS and HTTPS-redirect checks. The Rust server checks host restrictions, answers CORS preflights and redirects plain HTTP requests on its Tokio thread before it considers the probe. `GET` and `HEAD` negotiate `application/json` and validate query syntax; other methods return `405` with `Allow: GET, HEAD`, unless CORS answers a preflight first. WSGI reads request bodies before the pipeline, so its body-size behavior differs from the Rust server for a body sent to this `GET`/`HEAD` endpoint. WSGI also starts lazily on the first request and uses its adapter-level `503` if startup fails. Since WSGI exposes a decoded `PATH_INFO` rather than the raw request path, percent-encoded aliases may be indistinguishable from the literal probe path there.

### Limits

* HTTP/1.1, plus HTTP/2 over TLS when `server.http2` is on (see below). No WebSocket or other protocol upgrades: an `Upgrade` header is ignored and the request is answered as plain HTTP, and the server never answers `101`. There is no cleartext HTTP/2 (neither prior knowledge nor `Upgrade: h2c`).
* A request line plus headers larger than `execution.max_header_bytes`, or a query longer than `execution.max_query_bytes`, is refused (`431`, `414`). A body larger than `execution.max_body_bytes` is refused with `413`, by `Content-Length` before any byte is read or, for chunked bodies, as soon as the total passes the limit. A client that sends nothing for `body_read_timeout` seconds (10 by default, never more than half of `execution.request_timeout`) gets `408`.
* A client that does not finish its request headers within `header_read_timeout` seconds (10 by default) is disconnected; the same timer closes idle keep-alive connections.
* TLS is optional: set `server.tls_cert_file` and `server.tls_key_file` (PEM; the key may be PKCS#8, PKCS#1 or SEC1) and the Rust server speaks HTTPS with rustls (TLS 1.2 and 1.3, the library's default cipher suites, no client certificates, ALPN `http/1.1`, or `h2` then `http/1.1` with `server.http2`). The files are read once at start, so a renewed certificate needs a restart (a rolling restart of the workers is enough). A plaintext client on a TLS port gets a TLS alert, never an answer, and the handshake must finish within `header_read_timeout`. Requests over TLS have the scheme `https`, so the HSTS policy applies. Granian takes the same two settings (the framework asks it for TLS 1.2 and up and HTTP/1.1 only, as the Rust server does, unless `server.http2` is on). A trusted proxy's `X-Forwarded-Proto` overrides the scheme, `http` included, on both servers. The handshake and the request head each have their own `header_read_timeout`, so a slow client can hold a connection slot for up to twice that time. Behind a proxy that terminates TLS, leave them unset and list the proxy in `trusted_proxies` instead.
* HTTP/2 is opt-in: set `server.http2 = true` together with the TLS files (it is a settings error without them, on both servers). The server then offers ALPN `h2` and `http/1.1` and serves each connection with the protocol it negotiated; a client that offers only `http/1.1`, or no ALPN, still gets HTTP/1.1. Requests reach the application unchanged, with `http_version` `2` in the ASGI scope; the `:authority` pseudo-header becomes the `Host` and wins over a `host` header that differs. Limits are fixed, not settings: 32 concurrent streams per connection (the 33rd is refused), a 64 KiB stream window, header lists bounded by `execution.max_header_bytes` and at most 100 header lines per request (`cookie` fields count once; more is answered `431`), and the h2 library's rapid-reset protections. A connection counts as one slot of `max_connections`, but every stream in flight uses backend capacity (the in-flight limit and the queue) like a request of its own, so size those limits for connections times 32. The server also drops a connection on which more than 100 streams end before their response completed (client resets) within a fixed 10-second window; it is closed without a `GOAWAY`. The count is per connection, not per client: a client can pass about twice the bound across a window boundary, and a new connection starts from zero. A connection with no open stream for `header_read_timeout` is closed gracefully (`GOAWAY`; a stream opened in the race with the `GOAWAY` is accepted and then cut when the shutdown timeout ends). A response that sends no data for `write_timeout` seconds (a client that keeps its flow-control window at zero, or stopped reading) also gets the connection a `GOAWAY`: hyper cannot reset one stream from outside, so the other streams have the shutdown timeout to finish and the stalled one is cut with the connection. Progress is tracked per response, so opening new streams does not hide a stalled one. Keep-alive pings every 30 seconds drop a peer that stopped answering. Set `write_timeout` above 32 x 16 KiB divided by the slowest client bandwidth you expect to serve: HTTP/2 shares the window between streams unevenly, so on a slow link each of 32 concurrent downloads can wait that long between two chunks and be taken for stalled while bytes still flow. A request with several `host` headers and no `:authority` is refused with `400`. A client that trickles the head of one stream while others are open is not bounded by `header_read_timeout`. On stop the server sends `GOAWAY`, lets streams in flight finish, and aborts what is left after the shutdown timeout. Not provided: cleartext HTTP/2 (h2c), server push, and WebSocket or `CONNECT` over HTTP/2. Granian with `server.http2` negotiates HTTP/2 itself (`http` mode `auto`), under its own limits.
* A client that stops reading a response for `write_timeout` seconds (30 by default) is disconnected; any progress restarts the timer.
* `max_connections` (10000 by default) bounds open connections. Requests waiting for the application are bounded by an in-flight limit and a queue; beyond them the answer is `503`.
* Requests with ambiguous framing (for example `Content-Length` together with `Transfer-Encoding`, repeated lengths, or a bare line feed in the head) are refused with `400` and the connection is closed.
* There is no access log: `server.access_log=true` is refused with `--server rust`.
* A client that disconnects does not cancel its running handler; the handler finishes and its answer is discarded.

### Settings that apply to one server only

`hinoki serve` refuses a setting the chosen server cannot honour, by name, and exits with status 2; it never ignores it or falls back to the other server.

| Setting | Rust server | Granian |
| --- | --- | --- |
| `host`, `port`, `workers`, `runtime_threads`, `backlog`, `trusted_proxies`, `request_id_header`, `trust_request_id` | applied | applied |
| `access_log` | refused when `true` | applied |
| `max_connections`, `header_read_timeout`, `body_read_timeout`, `write_timeout` | applied when set | refused when set |

If the native extension of a build has no Rust server, `--server rust` fails with an error that says so.

### Workers, signals and restarts

With one worker `hinoki serve` runs the server in its own process. With several workers (and always under `hinoki dev`) it starts a supervisor that opens the listening socket once (honouring `backlog`), starts each worker as a fresh process that imports the target and builds the application, and passes the socket down. Workers share the one socket, so connections are spread by the operating system's accept; Hinoki does not balance them. Databases and pools open inside each worker's lifespan, never in the supervisor.

* `SIGTERM`, `SIGINT` (Ctrl-C) or `SIGHUP` stops accepting, lets running requests finish for up to `lifecycle.shutdown_timeout`, runs the lifespan shutdown and exits. A worker still running five seconds after that is killed. A second signal kills the workers immediately. Once the stop begins the supervisor closes its own copy of the socket, so a client that connects while the workers drain is refused. `--workers` must be 1 to 256 (`0` uses `server.workers`).
* A worker that exits on its own, whether it failed to start or crashed, is not restarted: the supervisor stops the other workers and exits with a non-zero status. Restart policy belongs to the process manager (systemd, a container runtime) that runs `hinoki serve`.
* The listening socket is opened before the lifespan startup of a supervised worker finishes, so connections wait in the backlog until a worker accepts them. With one worker the socket is opened after a successful startup.
* If the supervisor is killed without a chance to clean up, its workers notice within a second of finishing their startup and stop. The workers' copy of the listening socket is not inherited by processes a handler starts.
* A socket that cannot be opened (address in use, a bad port or host) is reported as `error: cannot listen on HOST:PORT: ...` with exit status 2, with one worker or several.

### `hinoki dev --server rust`

The supervisor watches the project directory (`*.py`, `*.toml` and `.env` files; `.venv`, `.git` and caches are ignored) and, on a change, stops the worker gracefully and starts a new one on the same socket, so a client connecting during the restart waits instead of being refused. If the new code fails to import or start, the error is printed (a short `error: cannot load ...` line for a missing module or name, a traceback for other failures), `waiting for changes` follows, and the next edit starts the worker again; connections wait in the meantime. The host, port, `backlog` and the choice of server are read once when `hinoki dev` starts: change them and restart the command. The other `server` settings (limits, `trusted_proxies`, `runtime_threads`) are read again by every restarted worker. The watcher ignores `.venv` but not a virtual environment with another name, such as `venv/`: keep it outside the project directory or it triggers reloads. The first import of the target happens in the command itself, so an error there stops it with a message instead of waiting. `dev` needs `watchfiles` (the `hinoki[server]` extra) and uses one worker.

### Behind a reverse proxy

`server.trusted_proxies` works as described in [settings](settings.md#reverse-proxies-and-request-ids): the native layer matches the TCP peer against it for the request ID and the HSTS and scheme policy of its own answers, and the request pipeline resolves the forwarded scheme and client address of the requests it handles from the same peer. Two points to know:

* Entries are IP addresses or CIDRs. An IPv6 address with a zone id such as `fe80::1%lo0` is refused when settings load, for either server (earlier versions loaded it and the ASGI path matched it; the Rust server cannot take it).
* On a listener bound to `::` that also accepts IPv4 (the default on many systems, for example Linux with `bindv6only=0` and macOS), an IPv4 peer appears as `::ffff:a.b.c.d`. A plain IPv4 entry such as `10.0.0.5` or `10.0.0.0/8` then never matches, and the proxy is treated as untrusted. That fails closed, and the ASGI path behaves the same. Either bind an IPv4 host (`--host 0.0.0.0`) or list the mapped form, for example `::ffff:10.0.0.0/104`.
* A request ID that a trusted proxy sent and Hinoki refused is logged as `ignored invalid request ID from trusted proxy`; for requests answered natively the warning is aggregated once a second with a count.

## Declaring where settings are read

The settings class says its own prefix and `.env` file, so nothing is repeated at the call site:

```python
from hinoki import settings

class BlogSettings(settings.AppSettings, prefix="BLOG_", dotenv=".env"):
    database_url: settings.Secret

app_settings = BlogSettings.load()          # reads BLOG_* and .env
```

## Other runners

`dev` and `serve` are conveniences. The application is a plain ASGI app, so `granian --interface asgi --factory blog.main:create_app` (or another ASGI server) works too; run that command from `examples/` with the locked environment active. The `server` group is ignored, because only `hinoki serve` applies it.

## WSGI

ASGI is the intended way to serve Hinoki. When a deployment can only run WSGI, wrap the application:

```python
# wsgi.py
from hinoki.wsgi import WSGIAdapter
from blog.main import create_app

wsgi_app = WSGIAdapter(create_app())
```

```console
$ granian --interface wsgi wsgi:wsgi_app
```

The adapter drives the same ASGI callable on a background event loop that it owns, so routing, binding, access, errors, limits and the OpenAPI document behave as under ASGI. Specifics:

* **Startup is lazy.** Modules (including the database pool) start on the first request of each worker process, so a pre-forking server can import the file in its master process. If startup fails, every request answers `503` with `not_ready` and the error is logged.
* **Shutdown** runs on `WSGIAdapter.close()` or, otherwise, at interpreter exit. A server that kills workers without exiting normally skips it, as it would any `atexit` hook.
* **Async handlers** run on the adapter's loop and **sync handlers** on the bounded executor, as under ASGI. Each WSGI server thread blocks for the duration of its request, so concurrency is the server's thread count rather than the event loop.
* **Bodies are buffered.** Request bodies are read up to `max_body_bytes` plus one byte, so an oversized upload still answers `413`. Responses are complete before they are sent; streaming responses are not available, and a client disconnect is not reported to the handler.
* **Paths.** The server decodes the path once and the adapter passes it on without decoding again, so a client's `%2520` reaches the handler as `%20`, as under ASGI. A percent-encoded slash (`%2F`) cannot be told apart from a real one in WSGI and is not available inside a path parameter. A bad `Content-Length` answers `400`.
* **One protocol per process.** Do not serve the same application object over ASGI and WSGI in one process; the adapter owns its own loop and module lifespan. Run two processes if you need both.

The path is checked against Granian's WSGI interface (`--interface wsgi`, with one and two workers), not against every WSGI server.
