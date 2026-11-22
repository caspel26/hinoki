# 📋 Release Notes

All notable user-facing changes to Hinoki are recorded here, newest first. Hinoki is pre-alpha, so any release may change the API and the migration format. Entries below `Unreleased` have not been published; this repository has no tagged release yet.

## [Unreleased]

---

### ✨ New Features

#### 🔄 Reload on SIGHUP in Kiso
> `python/hinoki/runtime/supervisor.py`, `python/hinoki/runtime/kiso.py`, `python/hinoki/runtime/wsgiworker.py`, `python/hinoki/runtime/listener.py`, `python/hinoki/serve.py`, `python/hinoki/settings.py`

`kill -HUP` on the supervisor of `hinoki serve` replaces the workers one at a time on the same listening socket: a new worker imports the code and reads the settings again, reports once its lifespan has started and it serves, and only then is the old one stopped gracefully. A new worker that does not start keeps the old ones running and the reload stops with an error. New setting `server.sighup` (`"reload"`, the default, or `"stop"` for the earlier behavior; Kiso only). `hinoki serve` now runs a supervisor even with one worker unless `sighup` is `"stop"`; on Windows, which has no `SIGHUP`, nothing changes. A worker that is stopping now answers requests already sent to it instead of refusing them with `503`, and a replacement worker's metrics use the next free `worker` number. See [reload on `SIGHUP`](docs/kiso.md#reload-on-sighup).

#### 📈 Prometheus metrics on Kiso
> `crates/kiso/src/metrics.rs`, `crates/kiso/src/scrape.rs`, `crates/bindings/src/metrics.rs`, `python/hinoki/settings.py`, `python/hinoki/serve.py`, `python/hinoki/runtime/supervisor.py`

Kiso can serve request metrics in the Prometheus text format from a dedicated listener (`metrics.listen`, off by default; Kiso only, and `hinoki serve --server granian` refuses it). Requests by route pattern, method and status class, duration histograms, requests in flight, response bytes and errors are counted in Rust with atomics; with several workers each keeps its series, told apart by a `worker` label. A `listen` address off the loopback needs `metrics.allow` or `metrics.token`. See [export Prometheus metrics](docs/metrics.md).

#### 🌐 Per-route CORS
> `python/hinoki/http/cors.py`, `python/hinoki/routing.py`, `python/hinoki/resources.py`, `python/hinoki/runtime/pipeline.py`, `python/hinoki/runtime/origins.py`, `python/hinoki/runtime/servercore.py`, `crates/bindings/src/server.rs`

`@http.get(..., cors=http.Cors(origins=(...)))` gives one action its own CORS policy, which replaces the application's for that route; `RouteGroup(..., cors=...)` sets a default for its actions and `crud(..., cors=...)` for the generated ones. A preflight is answered for the route named by `Access-Control-Request-Method` and the path (Kiso decides it natively), without running `access`, a transaction or the handler. See [CORS per route](docs/web-security.md#cors-per-route).

#### 🗜️ Response compression on Kiso
> `crates/kiso/src/compress.rs`, `crates/kiso/src/encode.rs`, `crates/bindings/src/compression.rs`, `python/hinoki/settings.py`, `python/hinoki/runtime/kiso.py`, `python/hinoki/serve.py`

Kiso can compress response bodies with brotli or gzip. It is off by default and Kiso only: `hinoki serve --server granian` refuses `compression.enabled`. A new `compression` settings group (`enabled`, `encodings`, `min_size`, `types`, `event_streams`, `files`, `gzip_level`, `brotli_quality` (0 to 9), `max_concurrent`) controls it. Kiso negotiates `accept-encoding`, adds `Vary`, skips small bodies, other content types and answers with `cache-control: no-transform`, flushes a stream whenever its producer pauses, and sends an answer uncompressed when `max_concurrent` compressions are already running. The release wheel grows with the two new Rust crates (flate2 and brotli). See [compress responses](docs/compression.md).

#### 🌊 Streaming responses
> `python/hinoki/http/stream.py`, `python/hinoki/runtime/streaming.py`, `crates/bindings/src/streamlimit.rs`, `crates/bindings/src/filelocate.rs`, `crates/bindings/src/tally.rs`, `crates/bindings/src/disposition.rs`, `python/hinoki/runtime/kiso.py`, `python/hinoki/runtime/asgi.py`, `python/hinoki/testing.py`, `crates/kiso`, `crates/bindings`

An action annotated `-> Stream`, `-> EventStream` or `-> File` is sent while it is produced, on Kiso and on Granian: `Stream` for bytes or text from an async or plain iterable, `EventStream` and `Event` for server-sent events, and `File` for a file that Kiso sends without its bytes entering Python. A slow client slows the producer down, a client that leaves closes the source, and a stream that fails after the head is cut instead of ending as complete. New settings `execution.max_streams` (64), `execution.stream_idle_timeout` and `execution.sse_keepalive` (15 s). `atomic=True`, a generator handler and status `204`/`304` are refused at registration; `WSGIAdapter` refuses an application with a streamed action when it starts, and the application MCP endpoint will not expose one. `ASGIClient.stream()` and `SyncClient.stream()` read the pieces as they are sent, and `client.get()` stops with `BodyTooLarge` on a body that does not end. See [stream a response](docs/streaming.md).

#### 🧭 Create an app at the end of `hinoki new`
> `python/hinoki/newcli.py`, `python/hinoki/appprompts.py`, `python/hinoki/scaffold.py` (#405, #408, #410)

`hinoki new` creates a project without models. In an interactive terminal it now asks whether to create the first app and reuses the same code as `hinoki app create`; `--app NAME` creates it without asking and `--no-app` skips the question. Project and app names may contain single underscores.

#### 📍 Docs URL when the dev server starts
> `python/hinoki/serve.py`, `python/hinoki/runtime/supervisor.py` (#411)

`hinoki dev` prints the documentation URL next to the server address.

#### 🎨 Admin theme and logo
> `python/hinoki/settings.py`, `python/hinoki/admin/core.py`, `python/hinoki/admin/authviews.py` (#399, #421)

The `admin.theme` setting selects the default admin theme, and the admin logo is configurable.

#### 🖥️ Terminal banner
> `python/hinoki/runtime/banner.py`, `python/hinoki/terminal.py` (#412, #415)

The Hinoki box appears on every colour terminal and shows Kiso's startup in the `hinoki new` box.

---

### 🔧 Improvements

#### ⚡ Less work per request
> `python/hinoki/schemafast.py`, `python/hinoki/schema.py`, `python/hinoki/runtime/pipeline.py`, `python/hinoki/runtime/binding.py`

A request now does less Python work with the same behaviour: a coroutine that finishes without suspending runs inline with no task and no timer, the `Request` object is built only when the action, a renderer or a body needs it, response header shapes are computed once per route shape, and a `Schema` whose fields hold exactly the declared types is accepted by a compiled per-class check instead of the full validation. A `Schema` that overrides `_validate_instance`, `native_schema` or `specs` keeps the full validation. Indicative single-core runs on a loaded host gave about +40% to +70% on a minimal action and +28% on a one-row read; the official comparative numbers are not yet re-measured.

#### 🔒 Hardened HTTP and WSGI edges
> `crates/kiso/src/conn.rs`, `crates/kiso/src/stream.rs`, `crates/bindings/src/wsgi.rs` (#389, #401, #402)

Middleware, raw responses and the WSGI path are stricter: a path with a NUL byte or a dot segment is refused, and a WSGI stream whose application goes silent is reset.

#### 🗄️ Database defaults and messages
> `python/hinoki/db.py`, `crates/orm/src/database.rs` (#395, #397)

The database pool defaults to the settings size of 10, and a URL scheme that lacks its slashes is named in the error.

#### 🧮 JSON is read and written by the Rust core
> `python/hinoki/jsontext.py`, `crates/bindings/src/request.rs`, `crates/bindings/src/modelrows.rs`

The command line, the migration state files, the JSON log format, the MCP server and the test client no longer use Python's `json` module. Output is the same data with compact separators (`{"a":1}`) where it was not indented, and floats such as `1e-07` print as `1e-7`. Command-line, log, MCP and migration-state output stays ASCII. What changes for callers:

- `NaN` and infinities are refused when written or read.
- Reading refuses more than 128 nested arrays or objects (the standard library allowed about 1000); this applies to MCP input and to the test client.
- The test client's `json=` also accepts `datetime` and `UUID` values.
- A JSON column whose bytes start with a UTF-8 byte order mark raises `FieldError`.

---

### 🐛 Fixes

- **Manifests and scaffolds:** names and paths with characters above the Basic Multilingual Plane (for example emoji) are written as valid TOML; they used to be written as surrogate-pair escapes, which TOML rejects (`python/hinoki/projectcli.py`, `python/hinoki/scaffold.py`).
- **`hinoki check`:** it imports the manifest's models before running the rules and warns when it ran no rules (`python/hinoki/checkcli.py`, #391, #393).
- **Migrations:** a plan error no longer suggests running `migrations plan` (#392).
- **Model validation:** text length errors count characters, not items (`python/hinoki/orm/model.py`, #396).
- **WSGI:** a plain WSGI callable reads its settings from the environment (`python/hinoki/runtime/wsgiworker.py`, #400).
- **JSON nesting limits:** reading accepts exactly 128 nested arrays or objects and writing exactly 200, as documented; the 129th and the 201st are refused. An empty innermost array or object used to count as one more, and a `Schema` object is now counted as a container (`crates/bindings/src/request.rs`).

---

### 📚 Documentation

The docsite has a new navigation, rules explorer, entry cards, footer and page transitions. The README is restructured around an animated request flow, the demo is rendered in HD at 60 fps, the comparative benchmark page is reorganized, and documentation links point at `hinoki.caspel.dev` (#388, #406, #409, #413, #416, #417, #419, #420, #423, #425, #426).

---

### 🎯 Summary

This is the running list of changes since the repository started. The first tagged release will be 0.1.0: `Unreleased` becomes `[0.1.0] - date`.
