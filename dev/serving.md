# Serve an application

From the project directory:

```sh
hinoki dev
hinoki serve
```

`hinoki dev` reloads on every change while you work; `hinoki serve` runs without
reload, with the workers your settings ask for. Both read the application from
`project.application` in `hinoki.toml`. To run another application, pass its
target: `module:app`, such as `hinoki dev notes.main:app`. A function callable
without arguments that returns an application (a factory) is also accepted.

On Kiso, when the application sets `docs_url`, `hinoki dev` prints the address of
the documentation page under the `listening on` line, for example
`docs: http://127.0.0.1:8000/docs`, with the address in the brand colour on a
terminal that takes colour (plain text in a pipe, in CI or with `NO_COLOR` set);
nothing is printed when `docs_url` is unset.
`hinoki serve` prints it too when it runs a supervisor (more than one worker),
but not with a single worker, which runs in the command's own process.

Each worker imports the target, then opens what the application owns (database
pool, modules) in its lifespan and closes it on shutdown. Importing your code
never opens a connection. See [database connection ownership](https://hinoki.caspel.dev/dev/settings.md#8-database-connection-ownership).

## Choose a server

Both commands run on **Kiso**, Hinoki's own Rust server, unless you choose
**Granian**:

| | Kiso (default) | Granian |
| --- | --- | --- |
| What it is | Hinoki's HTTP server in the native extension (Rust: hyper, tokio, rustls) | A general-purpose ASGI server, installed with the `server` extra |
| Runs on the native lane | Eligible routes skip Python routing and binding ([details](https://hinoki.caspel.dev/dev/kiso.md#what-is-native-and-what-still-runs-in-python)) | Every request goes through the ASGI pipeline |
| Access log | Kiso's own, with `access_log_format`, `access_log_skip`, `server_timing` | Granian's |
| Hinoki [middleware](https://hinoki.caspel.dev/dev/middleware.md) | Yes; the route runs in Python instead of the native lane | Yes, identical |
| ASGI wrapper around the app | Not possible: there is no ASGI boundary | Yes, run the wrapper with `granian` ([metrics](https://hinoki.caspel.dev/dev/serving.md#metrics-and-tracing)) |
| WSGI | `--interface wsgi`: Python threads behind Kiso's listener, not native ([WSGI](https://hinoki.caspel.dev/dev/serving.md#wsgi)) | `--interface wsgi` |

Choose per run with a flag, or per project in settings:

```sh
hinoki serve --server granian
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
server = "granian"
```

`hinoki new --server granian` (or the server question of `hinoki new`) writes the
same choice to `settings.toml`; `NOTES_SERVER__SERVER=kiso` overrides it for one
environment. What Kiso runs natively, its limits, signals and proxy handling are
on [Kiso server](https://hinoki.caspel.dev/dev/kiso.md).

## Set host, port and workers

They come from the application's `server` settings group; flags override them:

| Setting | Variable | Default | Flag |
| --- | --- | --- | --- |
| `server.host` | `NOTES_SERVER__HOST` | `127.0.0.1` | `--host` |
| `server.port` | `NOTES_SERVER__PORT` | `8000` | `--port` |
| `server.workers` | `NOTES_SERVER__WORKERS` | `1` | `--workers` (serve) |
| `server.runtime_threads` | `NOTES_SERVER__RUNTIME_THREADS` | `1` | |
| `server.backlog` | `NOTES_SERVER__BACKLOG` | `1024` | |
| `server.access_log` | `NOTES_SERVER__ACCESS_LOG` | unset | Unset logs in `hinoki dev` only (or whenever `access_log_format` is set); `true` or `false` decides for both |
| `server.access_log_skip` | `NOTES_SERVER__ACCESS_LOG_SKIP` | unset | Kiso only: comma-separated paths the log leaves out (health checks); exact, or a prefix when the entry ends with `*` |
| `server.server_timing` | `NOTES_SERVER__SERVER_TIMING` | unset | Kiso only: add a `Server-Timing` header (`kiso;dur=3.2;desc="native"`: milliseconds the server took and the serving lane, with the same values as the access log's [`lane`](https://hinoki.caspel.dev/dev/kiso.md#limits)); unset means `hinoki dev` only |
| `server.recent_requests` | `NOTES_SERVER__RECENT_REQUESTS` | unset | Kiso only: file that also receives each request as a JSON line, read by the `recent_requests` tool of [`hinoki mcp`](https://hinoki.caspel.dev/dev/dev-mcp.md); rotated near 1 MiB |
| `server.access_log_slow_ms` | `NOTES_SERVER__ACCESS_LOG_SLOW_MS` | unset | Kiso only: keep only answers at least this slow, in milliseconds |
| `server.access_log_format` | `NOTES_SERVER__ACCESS_LOG_FORMAT` | unset | Kiso only: `pretty`, `plain`, `json` or `logger`. Unset is `pretty` on a colour terminal and `plain` otherwise |
| `server.reuse_port` | `NOTES_SERVER__REUSE_PORT` | unset | Kiso only: on Linux, multi-worker `hinoki serve` on a non-zero port gives each worker its own `SO_REUSEPORT` socket by default; set `false` to share one descriptor everywhere |
| `server.server` | `NOTES_SERVER__SERVER` | `kiso` | `--server` |

The connection limits and timeouts (`max_connections`, `header_read_timeout`, `body_read_timeout`, `write_timeout`), `native_dispatch`, the TLS files and `http2`, and `trusted_proxies` are described under [Kiso](https://hinoki.caspel.dev/dev/kiso.md) and in the [settings table](https://hinoki.caspel.dev/dev/settings.md#built-in-fields-and-defaults). Left unset, they are `None` in `hinoki settings` and Kiso applies its own defaults (for example 10000 connections and a 10-second header timeout), so that Granian can refuse the Kiso-only ones only when you set them.

`dev` always runs one worker with reload. `hinoki settings` shows what is in
effect ([Inspect an application](https://hinoki.caspel.dev/dev/inspect.md#settings)).

Before serving a deployment, narrow [`allowed_hosts`](https://hinoki.caspel.dev/dev/web-security.md#allowed_hosts)
to the names it answers to (`NOTES_ALLOWED_HOSTS=app.example.com`). While it is
the default `*` and `debug` is off, each worker logs `allowed_hosts is '*' and
debug is off: ...` at startup, on every server.

## Settings that apply to one server only

`hinoki serve` refuses a setting the chosen server cannot honour, by name, and exits with status 2; it never ignores it or falls back to the other server.

| Setting | Kiso | Granian |
| --- | --- | --- |
| `host`, `port`, `workers`, `runtime_threads`, `backlog`, `access_log`, `trusted_proxies`, `request_id_header`, `trust_request_id`, `tls_cert_file`, `tls_key_file`, `http2` | applied | applied |
| `max_connections`, `header_read_timeout`, `body_read_timeout`, `write_timeout`, `native_dispatch`, `reuse_port`, `access_log_format`, `access_log_skip`, `access_log_slow_ms`, `recent_requests`, `server_timing` | applied when set | refused when set |

If the native extension of a build has no Kiso, `--server kiso` fails with an error that says so.

## Optional startup probe

`Hinoki(startup_url="/health")` reserves one public, fixed `GET`/`HEAD` endpoint and returns `{"status":"ok"}` after that worker's lifespan startup has completed. The setting is off by default. Its path must be a literal ASCII path; query strings, fragments, parameters, percent escapes and dot segments are rejected. It must not match an application route or an enabled OpenAPI/docs path. The endpoint is not an application route and does not appear in OpenAPI or generated CRUD/MCP tools.

This is a **startup probe**, not a general readiness or liveness check. A `200` says that startup completed (and, on Kiso, its request wake reader was installed); it does not check database availability, downstream services, request capacity, or whether the Python event loop continues to make progress. It answers independently of the Python request queue when Kiso can apply the response directly. Before readiness it returns `503 not_ready`; during shutdown readiness is cleared before graceful draining begins.

ASGI/Granian and WSGI answer it in the Python pipeline after host, CORS and HTTPS-redirect checks. Kiso checks host restrictions, answers CORS preflights and redirects plain HTTP requests on its Tokio thread before it considers the probe. `GET` and `HEAD` negotiate `application/json` and validate query syntax; other methods return `405` with `Allow: GET, HEAD`, unless CORS answers a preflight first. WSGI reads request bodies before the pipeline, so its body-size behavior differs from Kiso for a body sent to this `GET`/`HEAD` endpoint. WSGI also starts lazily on the first request and uses its adapter-level `503` if startup fails. Since WSGI exposes a decoded `PATH_INFO` rather than the raw request path, percent-encoded aliases may be indistinguishable from the literal probe path there.

## Other runners

`dev` and `serve` are conveniences. The application is a plain ASGI app, so any
ASGI server can run it directly; the `server` settings group is then ignored,
because only `hinoki serve` applies it:

```sh
granian --interface asgi notes.main:app
```

## Metrics and tracing

Kiso exports Prometheus metrics (off by default; see [export Prometheus metrics](https://hinoki.caspel.dev/dev/metrics.md)). Hinoki ships no tracing integration and no health route besides the optional [startup probe](https://hinoki.caspel.dev/dev/serving.md#optional-startup-probe). On an ASGI server such as Granian there is no built-in exporter, and the application is a plain ASGI callable, so wrap it with the instrumentation you already use, or with a few lines of your own:

```python
# notes/metrics.py
import time


class Timing:
    """Report each request's method, status and duration to ``record``."""

    def __init__(self, app, record):
        self.app, self.record = app, record

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started, status = time.monotonic(), None

        async def watch(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, watch)
        finally:  # status stays None when the request was aborted before a response began
            self.record(scope["method"], status, time.monotonic() - started)


from notes.main import app as notes_app

app = Timing(notes_app, record=my_metrics.observe)  # serve with any ASGI server
```

What this measures is the time from the ASGI call to the last byte handed to the server, not what a client perceives. Keep method and status as labels; do **not** label by `scope["path"]` (it holds ids and has unbounded cardinality), the request ID (it is for correlating logs: `logging.include_request_id` adds it to each Hinoki log line) or the caller. Hinoki does not currently expose the matched route template to a wrapper, so per-route metrics need an application-level counter inside the handler or a gap report so a hook can be designed.

Kiso has no ASGI boundary, so this wrapper does not apply to it. Use its
built-in [Prometheus metrics](https://hinoki.caspel.dev/dev/metrics.md) instead.

## Streamed responses

Actions that return `Stream`, `EventStream` or `File` work on both servers. Kiso sends them from its own queue, with its idle timeout, keep-alive comments and disconnect check, and sends a `File` without the bytes entering Python. Granian receives each piece as an ASGI message, and a `File` through `http.response.pathsend` when the server offers it; there Hinoki's own pump handles the idle timeout, the keep-alive and the disconnect. `execution.max_streams`, `execution.stream_idle_timeout` and `execution.sse_keepalive` apply to both ([settings](https://hinoki.caspel.dev/dev/settings.md)). Details and measured behavior: [stream a response](https://hinoki.caspel.dev/dev/streaming.md). Response compression exists on Kiso only; `compression.enabled` with `--server granian` is refused ([compress responses](https://hinoki.caspel.dev/dev/compression.md)).

## WSGI

ASGI is the intended way to serve Hinoki. When a deployment can only run WSGI,
wrap the application:

```python
# wsgi.py
from hinoki.wsgi import WSGIAdapter

from notes.main import app

wsgi_app = WSGIAdapter(app)
```

```sh
hinoki serve wsgi:wsgi_app --interface wsgi                    # Kiso
hinoki serve wsgi:wsgi_app --interface wsgi --server granian   # Granian
```

`--interface wsgi` (or `server.interface = "wsgi"` on the application the
`WSGIAdapter` wraps) tells `hinoki serve` that the target is a WSGI callable
rather than a Hinoki application; the default is `asgi`. The target may be a
`WSGIAdapter` or any `(environ, start_response)` callable, but not a factory
and not a `Hinoki` application (the error names `WSGIAdapter`). A project
whose `server.interface` is `wsgi` refuses an ASGI target unless you pass
`--interface asgi`. `granian --interface wsgi wsgi:wsgi_app` still works for a
deployment that runs Granian directly.

### WSGI on Kiso

Kiso owns the listener and calls the callable from a fixed pool of Python
threads (`execution.sync_workers`) with a queue of `execution.sync_queue`
waiting requests; beyond both the answer is `503` with `Retry-After: 1`. **This
path is not native**: the application, the environment and every response chunk
are Python objects, and the request crosses the interpreter lock. Rust owns the
sockets, TLS, the access log, the limits and the end of the response.

* **Request body.** Read in full before the call, up to `execution.max_body_bytes`: `413` above it, `408` if it stalls, `400` if malformed. Kiso therefore answers `413` for a body Granian's WSGI interface would pass on (Granian has no limit).
* **Request path.** A path whose decoded form holds a NUL byte or a `.` or `..` segment (`%00`, `%2e%2e`, `%2f` included) answers `400` before the application is called.
* **Request headers.** Each becomes `HTTP_*` in the environment, and repeats are joined by a comma; repeated `Cookie` headers (one per cookie, as HTTP/2 clients send them) are joined by `; `. A header whose name contains an underscore is dropped, because `X_Foo` and `X-Foo` would both become `HTTP_X_FOO`; a client could otherwise pose as a header your proxy sets, such as `Remote-User`. Use dashes in custom header names.
* **Response.** Streamed: the head goes out with the first piece, a slow client slows the application down (a bounded buffer), and a client that leaves makes the next write fail and closes the iterable. An exception before the head answers `500` with an empty body and the traceback in the log; after it, the stream is reset instead of ending as complete. A header value with a line break or another control character is an error in `start_response`, so it answers `500` with the traceback too. A body shorter or longer than the `Content-Length` the application set closes the connection with an error line from Kiso. A `close()` of the iterable that raises after the whole body was sent is logged by the `hinoki.runtime` logger, and the response still ends normally.
* **Timeout.** `execution.request_timeout` bounds the time until the response starts (`504`) and, once it has started, the silence between two pieces of the body: an application that stays quiet longer has its stream reset and its iterable closed. A slow client does not count, only a slow application. A running sync call cannot be stopped; it keeps its thread until it returns.
* **Applies:** TLS and HTTP/2, `access_log` and `access_log_format`, `server_timing`, `max_connections`, header and body timeouts, `--workers` and graceful shutdown (`lifecycle.shutdown_timeout`).
* **Does not apply:** the native lane (`native_dispatch` is refused), host, CORS and redirect policies of the server (a wrapped Hinoki application still applies its own), and `trusted_proxies` (refused: the client address is the TCP peer).
* **Settings.** A plain callable has no settings of its own, so `server`, `execution` and `lifecycle` come from the environment (`EXECUTION__SYNC_WORKERS`, `SERVER__WORKERS`, ...); a bad value stops the command with status `2`. A `WSGIAdapter` uses its application's settings.
* **Lifespan.** A plain callable has none. A `WSGIAdapter` starts its application's modules when the worker starts, not on the first request, and stops them on a graceful stop.

The adapter itself buffers its responses, so streaming reaches the client only from a plain callable. Its other specifics below apply on both servers.

The adapter drives the same ASGI callable on a background event loop that it owns, so routing, binding, access, errors, limits and the OpenAPI document behave as under ASGI. Specifics:

* **Startup is lazy** under Granian (Kiso starts them with the worker). Modules (including the database pool) start on the first request of each worker process, so a pre-forking server can import the file in its master process. If startup fails, every request answers `503` with `not_ready` and the error is logged.
* **Shutdown** runs on `WSGIAdapter.close()` or, otherwise, at interpreter exit. A server that kills workers without exiting normally skips it, as it would any `atexit` hook.
* **Async handlers** run on the adapter's loop and **sync handlers** on the bounded executor, as under ASGI. Each WSGI server thread blocks for the duration of its request, so concurrency is the server's thread count rather than the event loop.
* **Bodies are buffered.** Request bodies are read up to `max_body_bytes` plus one byte, so an oversized upload still answers `413`. On Kiso the bytes of the bodies being read are bounded by what the threads and the queue could hold (`(threads + queue) * max_body_bytes`); a request that would pass it answers `503` before its body is read. Responses are complete before they are sent: an application with an action that returns `Stream`, `EventStream` or `File` is refused when the adapter starts (`WSGIStartupError` naming the actions; serve it with ASGI or Kiso, see [streaming](https://hinoki.caspel.dev/dev/streaming.md)), and a client disconnect is not reported to the handler.
* **Paths.** The server decodes the path once and the adapter passes it on without decoding again, so a client's `%2520` reaches the handler as `%20`, as under ASGI. A percent-encoded slash (`%2F`) cannot be told apart from a real one in WSGI and is not available inside a path parameter. A bad `Content-Length` answers `400`.
* **One protocol per process.** Do not serve the same application object over ASGI and WSGI in one process; the adapter owns its own loop and module lifespan. Run two processes if you need both.

The path is checked against Kiso and Granian (`--interface wsgi`, with one and two workers) by a parity test, not against every WSGI server.

## Going to production

Before the first deployment:

- Run `uv run hinoki check` and `uv run pytest`; both must pass.
- Set `debug` off and narrow [`allowed_hosts`](https://hinoki.caspel.dev/dev/web-security.md#allowed_hosts) to your host names.
- Serve over HTTPS, with the TLS files or a proxy listed in `trusted_proxies`.
- Give secrets (`Secret` settings, `cursor_secret`) through the environment, never the repository.
- Apply migrations once per deployment, from one process, not from every worker's startup.
- Set `workers` and the connection limits for the machine, and check `GET /health` after the rollout.

## Errors and edge cases

- `error: X is neither an application nor a factory`: the target names something else, for example an ASGI wrapper; run wrappers with `granian`.
- `error: Granian is not installed; install the server extra`: run `uv sync` in a generated project (it depends on `hinoki-api[server]`), or use Kiso.
- `hinoki dev` reloads with `watchfiles`, also part of the `server` extra, on either server; without it `dev` exits with that message and `serve` still works.
- `--workers` outside the supported range is refused on Kiso; `0` means "use `server.workers`".
- `--interface wsgi` with `server.native_dispatch` or `server.trusted_proxies` set on Kiso exits with status `2` and names the setting.
- `--interface wsgi` with a `Hinoki` application or a non-callable exits with an error that points to `WSGIAdapter`. A factory exits with status `2`: `error: wsgi:create_app is a factory; for --interface wsgi name a WSGI callable, such as WSGIAdapter(app) from hinoki.wsgi`. Build the application at import time instead, as in `wsgi_app = WSGIAdapter(create_app())`.
- A Kiso-only setting under `--server granian` exits with status `2` and names the setting.

## See also

- [Kiso server](https://hinoki.caspel.dev/dev/kiso.md)
- [Settings](https://hinoki.caspel.dev/dev/settings.md#built-in-fields-and-defaults)
- [Command line](https://hinoki.caspel.dev/dev/cli.md)
