# Rust HTTP server (`hinoki serve --server rust`): local design, not committed

Status: draft for owner review, 2026-11-15. Covers roadmap stages R5-R9 of the current step
(handoff "Scope of the current step") and the later default-server flip. **No code has changed.**
Nothing here is implemented; every performance statement is either a number already recorded in
`handoff.md` / `docs/ADAPTER-PREPARED-PLAN-DESIGN.md` (with its caveats) or something that must
still be measured.

Already decided by the owner, not reopened here: the Rust server runs alongside Granian (ASGI) and
the WSGI adapter, which stay as parity and A/B references; base = hyper + tokio sharing the ORM's
tokio runtime; network scope HTTP/1.1, TLS (rustls), HTTP/2, WebSocket, staged, HTTP/1.1 first;
Hinoki stays independent of other web frameworks; every benchmark table shows "Hinoki · Granian"
and "Hinoki · Rust server" as two stacks. New owner requirement (2026-11-15, relayed): the Rust
server becomes the **default** for both `hinoki dev` and `hinoki serve` once its gates pass, with
Granian still selectable (`--server granian`) as the reference.

---

## 0. Owner decisions (genuine ones only; each has a recommended default)

| # | Decision | Recommended default |
| --- | --- | --- |
| D1 | **Gate for R5 alone.** A Rust dispatcher/prepared plan *under Granian* was already measured as not worth it (at most ~0.5 µs, ~2%, below the detector; ADAPTER design, "Stage 2 decision"). | R5 is an **enabler for R6**: its own gate is *no regression* (5 vs 5, 15%, 2σ) on the ASGI path plus parity; its gain is measured only together with R6 on the Rust server. On the ASGI path R5 is a behaviour-preserving refactor into `pipeline.py`; no native plan is used there unless it later passes a gain gate of its own. |
| D2 | **Definition of the R6 gate.** The only recorded upper bound (scratch prototype, sync handler, no asyncio, one thread) was −13.8% per request, which is *below* the 15% threshold; the detector is a regression detector, not an improvement bar. | R6 passes when (a) the parity suite is green on ASGI, WSGI and Rust, (b) no scenario of the same harness regresses vs "Hinoki · Granian" per the detector, and (c) served `/health` and the other no-DB scenarios improve with **disjoint ranges over 5 alternating pairs** and CV ≤ 15%; DB routes (`detail_protected`) need only no regression, since the adapter is about 5% of such a request. A missing improvement is reported, not forced; the owner then keeps the server as an option or stops. |
| D3 | **R6.0 spike before R6.** | Yes. A throwaway measurement (not merged) of the *async* handoff tokio → asyncio loop → tokio vs Granian on `/health` with an `async def` handler. If the Rust server is not faster than Granian on that, stop before building R6 and report. The spike also runs a DB route with `runtime_threads` 1 and 2 (section 3.3). |
| D4 | **R9 WebSocket scope.** Hinoki has **no public WebSocket API today** (no `websocket` anywhere under `python/hinoki`; README: "Streaming responses and WebSockets are not available"). | R9 = **transport only**: the Rust server cleanly refuses `Upgrade: websocket` (426/400 JSON) as Granian+ASGI adapter does today (the adapter raises on non-`http` scopes). A public WebSocket API is a **separate design** (`docs/WEBSOCKET-DESIGN.md`) that adds the ASGI `websocket` scope too, so Granian remains its parity reference; R9 proper starts only after that design is approved. |
| D5 | **Name and default flip.** CLI value `--server rust` (provisional) and the condition for making it the default of `dev` and `serve`. | Values `rust` and `granian`. Default flips only at build step F (section 9) after the R6 gate passes (R7 only if the owner wants TLS in `hinoki serve`: today `serve.py` passes no TLS options to Granian, so TLS is a new feature, not parity), the dev reload path is green, and wheels with the server exist for every platform Hinoki publishes. Until then default = `granian`. |
| D6 | **When the Rust server is not available** (wheel built without it, unsupported platform). | **Clear error, no silent fallback** when the user asked `--server rust` explicitly. When the server came from the *default*, fall back to Granian **only if installed**, with one warning line naming the reason; otherwise exit 2 with an install hint. (Rationale in section 8.4.) |
| D7 | **Dependency shape after the flip.** | Keep `hinoki[server]` = Granian + watchfiles extra; the Rust server ships inside the core wheel (same `hinoki.core` extension, no extra). Reload on the Rust server uses `watchfiles` too, so `hinoki dev` still needs `hinoki[server]` *or* a new `hinoki[dev]`; recommend renaming nothing now and adding `watchfiles` to a new `hinoki[reload]` extra included by `hinoki[server]`. |

Everything else below is routine and carries a stated default.

---

## 1. Observed code and data

### 1.1 Request path today (main `5921b0c`, plus R1 on `perf/rust-request-plan`)

* `hinoki serve` (`python/hinoki/serve.py`) builds the app once in the CLI process to read
  `ServerSettings` (`host`, `port`, `workers`, `runtime_threads`, `backlog`, `access_log`,
  `trusted_proxies`, `request_id_header`, `trust_request_id`), then hands the target string to
  `Granian(..., interface=ASGI)`. `hinoki dev` is the same with `reload=True`, `workers=1`, and
  requires `watchfiles`. Granian is the `hinoki[server]` extra (`granian[reload]>=2.8,<3`).
* `python/hinoki/runtime/asgi.py` `ASGIAdapter` (one per worker): `_lifespan` (startup/shutdown via
  `Lifespan`, StartupError → `lifespan.startup.failed`), `_http`: `effective_scope` (trusted
  proxy), request id (trusted inbound or `prefix(16 hex, secrets.token_hex(8)) + counter(16 hex)`),
  three `ContextVar` set/reset pairs (`request_id`, `sync_runner`, `database_var`) plus
  `route_headers`, `_respond` under `asyncio.timeout(request_timeout)` (504 only if the limit
  expired, otherwise handler `TimeoutError` is translated), `ExecutorSaturated` → 503
  `RETRY_LATER`, everything else → `error_response` (builtin → registry → 500 with request id,
  debug details if `debug`).
* `_serve` order (this is the parity contract): not ready 503 → query size 414 → header size 431
  (name+value+4 per header) → early policies (host check, CORS preflight, HTTPS redirect) →
  OpenAPI document → docs pages (GET/HEAD) → route (404 / 405 with `allow`) → `authorize` (needs
  headers + parsed query) → `negotiate` (406) → body only for `BODY_METHODS` (declared
  Content-Length > limit → 413 before reading; streamed total > limit → 413; disconnect →
  `InvalidBody`) → `build_request` (Python `Request`, `Headers`, query dict, path dict) →
  `_invoke` (transaction wraps `_call`; rollback before error translation) → `_call` (async
  handler awaited on the loop; sync handler on `SyncExecutor`).
* Response assembly: policy headers per scheme (precomputed), route headers, request id, CORS
  headers unless the handler set `access-control-allow-origin`, `content-length` unless status is
  bodyless, HEAD sends an empty body **with the GET content-length**. R1 (commit `003d63a` on
  `perf/rust-request-plan`, worktree `.claude/worktrees/adapter-stage2`, not on main) moves header
  assembly to `core.response_headers(...)` and the header dict + size to `core.request_headers`.
* `python/hinoki/wsgi.py`: a compatibility path that owns a background asyncio loop thread and
  drives each request through the same ASGI callable; buffered; never reports disconnect.
* Rust: `crates/bindings` (PyO3 0.30, `abi3-py312`, `extension-module`) exposes Router, parsers
  (`parse_query_string`, `header_dict`, `negotiate_media`, multipart, JSON decode/encode), schema,
  crypto, JWT, the DB bridge. `crates/orm` is pure Rust (tokio-postgres + deadpool, tokio-rusqlite,
  mysql_async; rustls 0.23 with the **ring** provider). `db.rs` creates **one tokio multi-thread
  runtime per process on first DB call** (`ensure_runtime`, default 1 worker thread,
  `configure_runtime` before first use) through `pyo3_async_runtimes::tokio::init`, and bridges
  futures with `future_into_py` (result delivered to the running asyncio loop). Locked: tokio
  1.54.1, rustls 0.23.46, pyo3-async-runtimes 0.30.0. hyper/h2 are **not** dependencies yet.
* No `unsafe` in `crates/*/src` (grep, 2026-11-15).
* ADR 0001 ("no second HTTP server for the first release") and ADR 0002 (ORM owns a dedicated
  tokio runtime per worker; pools after start; no `block_on` from loop threads) are the decisions
  this design changes or extends; both need a successor ADR (step 7 of section 9).
* CI: Ubuntu runners; `cargo clippy -D warnings`, `rust-code-analysis-cli 0.0.25` complexity check,
  `cargo test --workspace`; `complexipy` for Python. Locally on macOS `cargo test` cannot link the
  `extension-module` crate (handoff, also on main).

### 1.2 Recorded numbers (only these; caveats attached)

All from macOS M4 Pro, release extension, Python 3.14.0, **noisy hosts** (load 3-7); proportions,
not publishable absolutes.

| Source | Measurement |
| --- | --- |
| ADAPTER design, observed | One Granian worker, 64 conn, `/health`: minimal ASGI 7.6 µs/req, minimal RSGI ≈1.5% faster, Hinoki 23.7 µs (Hinoki layer ≈16 µs). |
| ADAPTER design, stage-2 decision (main `f2dbdeb`, load 6-7) | In process `health` 11.5 µs; served minimal ASGI 8.8 µs, Hinoki 20.9 µs; foldable Rust candidates ≈1.2 µs, net ≤ ≈0.5 µs (≈2%). |
| Handoff, adapter stage 1 | Served by one Granian worker, 5 pairs × 15 s: 31 244 → 36 660 req/s (+17%, ranges disjoint, CV 0.9%). |
| Handoff, R1 investigation (5 alternating rounds, `py_cpu_64`) | health min 11.1 → header-only 10.5 µs; `--prod` 20.4 → 14.9 µs (−27%); `detail_protected --prod` 100.3 → 94.2 µs (−6%); `Router.prepare` adds only 0.3-0.7 µs. |
| Handoff, direct-server prototype (scratch, not merged) | Minimal Rust direct-dispatch server, **sync handler, tokio and Python on one thread, no lifespan/backpressure/limits**: 8.79 µs (Granian ASGI minimal) → 7.58 µs (−13.8%); 7.08 µs with a no-arg handler. Upper bound only; async variant never measured. |
| Handoff, DB requests | DB wait dominates single requests (~260-270 µs `detail`, ~1.8 ms list); adapter ≈13 µs ≈ 5% of a whole `detail` request. |

Tenant-suite and smoke numbers are explicitly marked "do not quote" in the handoff and are not
used. Consequence that must be said plainly: **Granian is itself hyper + tokio**, so the A/B does
not compare HTTP stacks; it compares Granian's ASGI bridge (scope dict, `receive`/`send`
messages, its own Python/tokio handoff) with Hinoki's direct bridge plus native early responses.
On DB routes the possible gain is a few percent at most; the honest expectation is "faster on
no-DB and policy-heavy routes, within noise on DB routes", to be measured.

---

## 2. Recommendation

Build a hyper 1.x + hyper-util server in a **new pure-Rust crate `crates/server`** (no PyO3, like
`crates/orm`), glued to Python by a thin `crates/bindings/src/server.rs`. Per worker process:

* The **main thread runs the asyncio loop** (owns signals, `asyncio.timeout`, lifespan, the
  `SyncExecutor`, the ORM's `future_into_py` completions). This keeps every asyncio-facing
  semantic exactly as under Granian (ADAPTER design stage 3: these stay Python).
* The **shared tokio runtime** (the one `db.rs::ensure_runtime` creates, sized by
  `ServerSettings.runtime_threads` through `configure_runtime`) runs accept, HTTP parsing, limits,
  timeouts, routing, the prepared plan and native early responses.
* **No GIL acquisition on tokio threads in the hot path.** The tokio side handles only Rust data;
  Python objects exist only on the loop thread. Handoff = a bounded Rust queue plus one wake fd
  registered with `loop.add_reader`; the fd is written only on the empty → non-empty transition
  and the loop callback drains a batch. Return = Python calls `prepared.respond(...)`, which
  completes a tokio oneshot.
* **One shared Python per-request pipeline** (`hinoki/runtime/pipeline.py`, extracted from
  `_http`/`_respond`/`_serve`) called by both the ASGI adapter and the Rust server, so error
  translation, timeouts, request id, context variables, transaction ordering and header assembly
  (R1's `core.response_headers`) cannot drift.
* Requests that need no Python (414, 431, not-ready 503, overload 503 always; 404/405, OpenAPI
  JSON and docs pages only when no host/CORS/HTTPS-redirect policy is configured, see 3.12) are
  answered **in Rust** with bodies rendered at startup by the Python side's own `HTTPError`, with
  the request id spliced in.

Alternatives considered:

| Alternative | Why not |
| --- | --- |
| Drive coroutines from tokio (`coro.send(None)` on a tokio thread, no asyncio loop) | Breaks `asyncio.timeout`, `asyncio.sleep`, any asyncio library a handler uses, and the ORM bridge which completes on the asyncio loop. Rejected. |
| `loop.call_soon_threadsafe` per request from tokio threads | Requires the GIL on a tokio thread per request (contention with the loop thread) and one self-pipe write per request. Kept only as the R6.0 spike baseline. |
| Sync handlers via `spawn_blocking` on tokio | Bypasses `SyncExecutor` admission control (503), `contextvars` copy and the executor drain at shutdown. Rejected for parity; revisit only with a measured reason. |
| actix-web / axum | Owner chose hyper + tokio; actix-rt is single-thread per worker with `!Send`; axum adds a router Hinoki already has. |
| RSGI on Granian | Measured ≈1.5% vs ASGI (ADAPTER design); same bridge cost. |
| Eager body read in Rust before Python | Flips 401/413 ordering and lets unauthenticated clients force `max_body_bytes` buffering. Rejected (section 3.4). |

---

## 3. The real boundary (what this is not: "entirely native")

### 3.1 Per request, async handler, route without body

Thread handoffs (irreducible while handlers run on the asyncio loop): **2** (tokio → loop,
loop → tokio). Python↔Rust calls on the critical path: **2** (the loop's drain callback enters
Python once per *batch*, then per request Python calls into Rust once to `respond`), plus whatever
the handler itself calls (ORM, schema, crypto) which is unchanged. Target "one crossing per
request" means: **one Python entry and one `respond`**, instead of today's ASGI `receive`/`send`
calls, `header_dict`, `response_headers`, `request_headers`, `Router.find`, `parse_query_string`.

Python objects still created per request (honest list): the `PreparedRequest` wrapper (a frozen
`#[pyclass]`), the coroutine and its `Task` (or eager-start result; `eager_task_factory` is
measured in R6, not assumed), the `request_id` string, context-variable tokens, the header `dict`
(lazily, only if the route needs headers: access, negotiation with >1 renderer, or a `Request`
parameter), the query `dict` (lazily), the path-params `dict`, the `Request` object when the
handler takes it, handler arguments, the handler's result, the rendered `bytes`, the `Response`,
the header list returned by `core.response_headers`. The README/docs must keep saying the
handlers and the adapter pipeline run in Python.

### 3.2 Flow

```
tokio thread                                   asyncio loop thread (main)
accept → hyper parse → limits (414/431)
→ request id (Rust counter, same format)
→ Router + plan → early native answers ──► write response (no Python)
→ enqueue PreparedRequest (bounded)  ──wake fd──► drain(batch): for each, create_task(pipeline(req))
                                                pipeline: ready? contextvars, timeout,
                                                authorize, negotiate, [await req.body()],
                                                build_request, invoke, encode, error translation,
                                                core.response_headers → req.respond(status, headers, body)
oneshot ◄────────────────────────────────────── respond copies body bytes into Bytes
→ hyper writes (HEAD: no body, GET content-length)
```

### 3.3 GIL handling

**Correction (2026-11-16, after reading `pyo3-async-runtimes` 0.30 `generic.rs`; supersedes the
earlier text of this section):** `future_into_py` does **not** take the GIL on a tokio worker. It
runs the future on a worker, then takes the GIL on a **`spawn_blocking` (blocking-pool) thread** and
resolves the asyncio future with `loop.call_soon_threadsafe`. The only GIL wait on a tokio
**worker** is `db.rs`'s `bridge!` macro: its `Python::attach` (rows to Python, `db_error`) runs
inside the future. So the goal "no GIL on tokio threads" is false today for the ORM, but the
problem is smaller than first written: one worker-side GIL wait per call, plus a blocking-pool hop
and a `call_soon_threadsafe` per call. With the owner's shared runtime and 1 worker thread, that
worker-side wait can still stall accept, parsing and response writes of that worker; its size must
be measured (R6.0 includes a DB route with `runtime_threads` 1 and 2). Details and the replacement
design: `docs/NATIVE-AWAIT-DESIGN.md`.

* **Mitigation (build step 3b):** native awaitables (ORM results and the request body) complete
  through the same bounded queue + wake fd as requests; the loop-side drain converts rows to Python
  and resolves the asyncio future. tokio threads then hold no GIL anywhere. Because `db.rs` is
  shared with the Granian path, this change is A/B measured on **both** servers (no regression on
  Granian, 5 vs 5). If the spike shows stalls and the mitigation is not adopted, the owner is told
  that the shared runtime costs throughput, with the numbers.
* Until 3b lands, the rule applies to the server code only: the HTTP path (accept, parse, limits,
  plan, early answers, write) never calls `Python::attach`. Startup (plan build) and shutdown
  attach on the main thread only.
* `respond` runs on the loop thread holding the GIL; it copies the body into `bytes::Bytes`
  (memcpy; avoids holding `Py<PyBytes>` that would later be dropped on a tokio thread) and sends
  the oneshot (no allocation of Python objects, no GIL needed on the receiver).
* `PreparedRequest` holds only Rust data; dropping it on any thread needs no GIL.
* Free-threaded CPython: the extension is `abi3-py312`, which free-threaded builds do not load;
  out of scope, stated in docs.

### 3.4 Body

`PreparedRequest.body()` returns a Rust awaitable (via `future_into_py` at first, via the
queue + wake fd after step 3b; see 3.3) that reads the hyper
`Incoming` body with the same rules as `_body` + `read_body`: declared Content-Length > limit →
413 before reading; streamed total > limit → 413; client gone → `InvalidBody("disconnected")`;
body read timeout → 408 (new, see 3.8). It is awaited by the pipeline **after** `authorize` and
`negotiate`, preserving order. Cost: protected body routes take a second crossing; "one crossing"
is claimed only for routes without body. The body stays in hyper's connection until pulled; hyper
sends `100 Continue` only when the body is first polled, which keeps today's semantics (no 100
before authorization).

### 3.5 Sync handlers

Unchanged: `pipeline` awaits `SyncExecutor.run` on the loop (bounded `sync_workers + sync_queue`,
`ExecutorSaturated` → 503). The Rust queue bound (3.7) sits in front of it.

### 3.6 Cancellation and client disconnect

* Request timeout: Python `asyncio.timeout(request_timeout)` exactly as today (504 vs handler
  `TimeoutError` distinction kept).
* Client disconnect while the handler runs: **default = parity, the handler is not cancelled**;
  its result is discarded when `respond` finds the oneshot receiver gone (`respond` returns
  `False`, logged at debug). Hinoki today notices a disconnect only inside `read_body`; what
  Granian does to a running ASGI task on disconnect is **unknown** and must be pinned by a
  real-socket test against Granian before claiming parity. Cancelling a handler mid-transaction is
  safe for rollback but not for side effects outside the DB, so cancel-on-disconnect stays off.
* hyper HTTP/1 may not observe a half-close while the service future is pending; with this default
  it does not matter. HTTP/2 `RST_STREAM` drops the service future; same handling.
* Shutdown cancellation: see 3.10.

### 3.7 Backpressure

* Per worker `max_connections` (semaphore around accept; when full, stop accepting so the kernel
  backlog — `ServerSettings.backlog` — absorbs and then refuses). Default 10 000 (routine).
* Bounded handoff queue + in-flight count `max_in_flight` (default `4 × (sync_workers +
  sync_queue)` = 288 with defaults, raised to at least the harness concurrency in benchmark configs; a benchmark block that shows any 503 is invalid, not a result). Over the bound → native 503 `overloaded` with
  `Retry-After` identical to the executor's, without entering Python.
* HTTP/1.1 pipelining: hyper serves one request at a time per connection; no extra buffering.

### 3.8 Limits and timeouts (defaults from `RuntimeConfig` where they exist)

| Item | Where | Behaviour |
| --- | --- | --- |
| `max_query_bytes` (8192) | Rust, before routing | 414 JSON identical to Python's. |
| `max_header_bytes` (32 768, name+value+4) | Rust, same formula | 431 JSON identical. hyper's own `max_buf_size` set to `max_header_bytes + 8 KiB` (request line margin) as a hard backstop; a request that hits the backstop gets hyper's bare 431 — documented difference, tested. |
| header count | hyper `max_headers` | default 100 (hyper default); configurable. |
| `max_body_bytes` (1 MiB) | Rust body awaitable | as 3.4. |
| header read (slowloris) | hyper `header_read_timeout` with `TokioTimer` | default 10 s, 408 / close. Whether it also covers the idle wait between keep-alive requests in the pinned hyper version must be **verified by a test**; if not, an explicit idle timer wraps the connection. |
| body read | tokio timeout around each body chunk | default 30 s → 408. |
| keep-alive idle | as above | default 5 s (routine). |
| response write | tokio timeout on the connection future after the response is ready | default 30 s, then close. |
| `request_timeout` (30 s) | Python, unchanged | 504. |

New settings live in a new `ServerSettings` subgroup only where `RuntimeConfig` has no equivalent
(`max_connections`, `max_in_flight`, `header_read_timeout`, `body_read_timeout`,
`keepalive_timeout`, `write_timeout`, later `tls_*`, `http2_*`). Under `--server granian` these
are **refused at startup** with a clear message if set to non-defaults (so a setting never silently
does nothing); every existing `ServerSettings` field is honoured by the Rust server
(`access_log` → one line per request from Rust via `log` → Python `logging` bridge only when on;
default off).

### 3.9 Lifespan parity

The Rust worker reuses `ASGIAdapter._lifespan` unchanged by feeding it `lifespan.startup` /
`lifespan.shutdown` messages through two `asyncio.Queue`s, the way `wsgi.py` already does. Startup
failure → worker exits non-zero before binding/accepting (matches the observed Granian 2.8.8
behaviour in ADR 0001). `lifespan.ready` is checked natively (Rust reads an `AtomicBool` the
Python side sets) so not-ready 503 needs no Python.

### 3.10 Graceful shutdown

SIGTERM/SIGINT on the loop: (1) stop accepting (`GracefulShutdown` from hyper-util, close
listener); (2) let in-flight requests finish up to `shutdown_timeout` (10 s), idle keep-alive
connections closed immediately, HTTP/2 GOAWAY; (3) remaining requests: Python tasks cancelled
(same as Granian killing the loop; exact Granian behaviour to be pinned by test), their connections
get 503 if headers not yet sent; (4) lifespan shutdown (modules, pools, executor drain); (5) tokio
runtime shut down with `shutdown_timeout`. A second signal exits immediately.

### 3.11 Worker / process model, fork safety

* `workers == 1`: everything in-process (no supervisor). `workers > 1`: a Python **supervisor**
  (the CLI process) binds the listening socket once (honours `backlog`, works on macOS where
  `SO_REUSEPORT` is not known to balance like Linux — to verify, not assumed), and **spawns**
  workers with `subprocess` + `pass_fds` (no `fork`), so no tokio runtime, ORM pool, or request-id
  prefix is ever inherited. Each worker imports the target, builds the app, creates the runtime and
  pools after start (ADR 0002 rule). The request-id prefix is generated per worker after spawn
  (closes the low risk the stage-1 review raised).
* The supervisor never initialises tokio: `server_settings()` builds the app, which does not reach
  `ensure_runtime` (only `Database.connect` does, grep 2026-11-15); a test asserts the runtime is
  not started in the supervisor.
* A worker failing startup → supervisor stops the others and exits non-zero. Crashed worker after
  startup: no respawn in R6 (routine default, documented); respawn with backoff is a later item.
* Linux `SO_REUSEPORT` per worker is a measured option later, not the default.

### 3.12 Request id, security headers, error translation, HEAD/Allow, docs pages

* Request id: generated in Rust with the same format (16-hex random per-worker prefix + 16-hex
  counter); trusted inbound id (`trust_request_id` + trusted proxy) validated by the same rules
  (`inbound_request_id` ported with a shared table-driven test).
* `effective_scope` (trusted proxies, `X-Forwarded-Proto/For`) needs the peer address and scheme:
  ported to Rust in the plan; scheme = `https` from R7.
* Security/policy headers, route headers, CORS, request id: Python path uses R1's
  `core.response_headers`; native early responses use the same Rust function body (shared
  internal fn, not a copy).
* Error bodies: `HTTPError` bodies contain the request id (`{"error": ..., "request_id": ...,
  **extra}` in `runtime/responses.py`), so they are **not** constant. The early-response bodies
  (404, 405 + `Allow`, 414, 431, 503 not_ready/overloaded) are rendered once at startup by the
  Python `HTTPError(...).response` with a sentinel id, split around the sentinel, and stored as
  prefix/suffix bytes; Rust splices the per-request id (32 hex, no JSON escaping needed). A parity
  test compares body bytes across ASGI/WSGI/Rust after substituting the id.
* **Order of policies.** In `_serve`, host check, CORS preflight and HTTPS redirect run *before*
  docs/OpenAPI and 404/405. Preflight answers depend on `Origin` and request headers, the
  redirect builds `Location` from `Host`, host refusal is a raised `HTTPError`; none is
  precomputable. **Default for R6:** when `hosts`, `cors` or `redirect_https` is configured, every
  request that is not rejected by 414/431/not-ready/overload goes through Python (so a bad-Host
  request to an unknown path still gets the host refusal, not 404). Native 404/405/docs/OpenAPI
  answers apply only when none of those policies is set. Porting the three policies to Rust would
  duplicate security logic in two languages; it is a later option needing shared table-driven
  tests and an Opus review, not part of R6.
* HEAD: answered from the GET route with the GET `content-length` and no body; hyper may
  recompute `content-length` from an empty body, so the response is built with an explicit
  header and checked with a raw-socket test.
* OpenAPI JSON and docs pages: precomputed bytes in the plan, served natively with the same
  headers as `docs_response` (CSP, `nosniff`, cache-control).

### 3.13 Complexity ≤ 15 (Rust and Python)

The connection path is split into small functions with one decision each: `accept_loop`,
`serve_conn` (builder config only), `check_limits` (table of checks returning `Option<Early>`),
`resolve` (router + plan lookup), `early_answer` (match on an enum, one arm per kind delegating to
a renderer), `enqueue`, `wake`, `respond`, `write_head`. Ordered policy checks are a slice of
`fn(&Req, &Plan) -> Option<Early>` iterated in order, so adding a policy adds a function, not a
branch. `rust-code-analysis` scores closures too; hyper `service_fn` closures only forward to named
functions. Python: `pipeline.py` keeps today's split (`_respond`, `_serve`, `_early`,
`_dispatch`), checked by `complexipy`.

---

## 4. Stages R5-R9

Common to every stage: own branch `perf/rust-server-rN` (R5) or `feat/rust-server-*`, scoped
commits (build, implementation, tests, docs separate), measured on **the same harness and the
same host session**, alternating A/B: `benchmarks/comparative/profile_request.py` (in-process,
`health`, `detail_protected`, `--prod`), `server_floor.py --workers 1` (gains a `hinoki-rust`
stack and a minimal Rust-server floor), and the tenant harness on `bench/tenant-comparative`
(`tenant_run.py` STACKS/FLOORS + `tenant_report.py` gain `hinoki-rust`; real runs only on the
quiet host the owner provides). **Detector policy: 5 baseline vs 5 current runs, 15% threshold,
2σ heuristic; CV > 15% → investigate load/reproducibility before any conclusion.** A stage that
fails its gate is reported, not forced; the next stage does not start until the owner decides.

| Stage | Content | Gate (A/B vs) | STOP when | Independent Opus review |
| --- | --- | --- | --- | --- |
| **R5** dispatcher + prepared plan | `pipeline.py` extracted from `asgi.py` (ASGI adapter calls it; behaviour unchanged); `crates/server` crate skeleton with `Plan` (per route: action, method set, param names, body method, has access, renderer media types, status, route headers, transaction, is_async, takes_request) built at startup from `Binding`s; native early answers; `PreparedRequest` pyclass; driven in tests by an in-process Rust harness (no socket). | vs main on ASGI: no regression (D1). In-process `profile_request.py` health/detail_protected. | Any ASGI scenario regresses per detector, or parity diff appears. | Yes: GIL and object lifetime in `PreparedRequest`/`respond`. |
| **R6.0** spike (throwaway, D3) | Minimal hyper server + queue/wake-fd handoff + `async def` handler, plus one DB route (ORM on the shared runtime) with `runtime_threads` 1 and 2 to size the GIL-stall risk of 3.3. | vs Granian minimal ASGI on `server_floor.py` 1 worker, 5 pairs. | Not faster than Granian with disjoint ranges → stop R6, report. | No (not merged). |
| **R6** HTTP/1.1 | Full server: limits, timeouts, keep-alive, backpressure, shutdown, supervisor, `--server rust`, `hinoki-rust` in all harnesses. | vs "Hinoki · Granian" (D2): parity green, no regression, no-DB scenarios (`/health`) improve with disjoint ranges; DB routes no regression only. Tenant P1/P3 slices when the quiet host exists. | Parity red, any regression, or no improvement → report; owner chooses keep-as-option or stop. | Yes: GIL, cancellation, shutdown, smuggling/limits. |
| **R7** TLS (rustls) | `tokio-rustls`, ring provider (same as ORM; one provider installed explicitly), TLS 1.2+1.3, ALPN `http/1.1` (+`h2` in R8), cert/key reload on SIGHUP: no (restart). | vs Granian with its TLS on the same cert: handshake rate and `/health` over TLS; no regression. | Regression, or any testssl/handshake defect. | Yes: TLS defaults, key handling. |
| **R8** HTTP/2 | hyper `http2` (h2), ALPN and h2c prior-knowledge off by default. | vs Granian HTTP/2 (`oha --http2`): no regression; limits behave (rapid-reset test). | Regression or a limit test fails. | Yes: stream limits, rapid reset, flow control. |
| **R9** WebSocket | Per D4: first only clean refusal of upgrades; the transport (hyper upgrade + `tokio-tungstenite` or hand-rolled framing) only after the public API design is approved, with ASGI `websocket` scope on Granian as reference. | Refusal: parity test only. Transport: echo/latency vs Granian ASGI websocket. | API design not approved → R9 ends at refusal. | Yes (framing, limits, origin checks). |
| **F** default flip | Section 8. | Section 8.2. | Any gate red. | Covered by R6/R7 reviews; flip diff reviewed normally. |

Notes: R5's gain is not claimed on its own (ADAPTER design already measured that path at ≈2%).
R6's prior upper bound is −13.8% (sync handler, no asyncio, scratch): that it might not reproduce
with async handlers is the reason for R6.0. On DB routes the expected effect is small (adapter
≈5% of a `detail` request); a "no regression" result there is the likely honest outcome.

---

## 5. Security surface

* **Request smuggling:** expected (to be pinned by test, RFC 9112 lets TE override CL rather than requiring rejection): hyper rejects or safely resolves requests with both `Content-Length` and
  `Transfer-Encoding`, conflicting duplicate CL, invalid chunk sizes, obs-fold; we additionally
  refuse `Transfer-Encoding` other than a final `chunked`, and do not forward anything (no proxy
  role). Each case is a **raw-socket test** (CL+TE, CL+CL differing, TE `chunked, identity`,
  bare LF line endings, chunk-size overflow, header names with spaces, NUL/CR in values) asserting
  400/close; the same tests run against Granian to document its behaviour (not to change it).
* **Header limits / slowloris / slow readers:** 3.8; max connections 3.7; per-IP limits are **out
  of scope** (belong to a proxy/LB; documented).
* **TLS defaults (R7):** rustls only (no OpenSSL), TLS 1.2 and 1.3, rustls default safe cipher
  suites, no client-auth by default, no session tickets across workers unless configured, ALPN set
  explicitly, key files read once at worker start with the process's permissions; private key never
  logged; HSTS remains the existing security-header setting.
* **HTTP/2 (R8):** pin hyper/h2 versions that include the CVE-2023-44487 rapid-reset mitigation
  (exact version numbers to be checked at R8 time, not assumed here); set
  `max_concurrent_streams` (default 100), max pending accept reset streams, max header list size =
  `max_header_bytes`, initial window sizes, keep-alive pings with timeout; builder method names
  verified against the pinned version. Test: a client opening and resetting streams in a loop must
  be disconnected.
* **Unsafe-code policy:** `crates/server` starts with `#![forbid(unsafe_code)]`; bindings glue has
  none either (none exist today). The one known spot that may need `unsafe` is adopting the
  listening socket the supervisor passes as a raw fd (`FromRawFd` is `unsafe`). Preferred: the
  worker binds nothing and receives the fd number; a safe adoption path is checked at R6 time; if
  none exists in the pinned versions, the single `unsafe` block lives in one reviewed function in
  the bindings glue with a `// SAFETY:` comment (ownership transferred exactly once), and
  `crates/server` keeps `forbid`. `cargo audit` stays in CI.
* **Information leaks:** native 500s never contain Rust panic text; a panic in a connection task is
  caught (`catch_unwind` at task boundary via `JoinHandle` result), logged with the request id,
  answered 500 if headers not sent.

---

## 6. Failure, empty and private states

| State | Behaviour |
| --- | --- |
| Rust server not compiled in the wheel / platform unsupported | D6: explicit `--server rust` → exit 2 "this build of hinoki has no Rust server (platform X)". Default → fall back to Granian if importable with one warning, else exit 2 with install hint. |
| Startup failure in a worker | Exit non-zero before accepting; supervisor stops others, exits non-zero. |
| Not ready (startup running) | Native 503 `not_ready` + Retry-After. |
| Overloaded queue | Native 503 `overloaded`. |
| Executor full | 503 from Python as today. |
| Port in use / bind failure | Supervisor exits 2 with the OS error, before spawning workers. |
| App with no routes | Plan empty; everything 404 natively; docs pages still served if enabled. |
| `debug=True` | Same debug 500 bodies (Python path); native early errors never carry tracebacks. |
| Private data | Request bodies, auth headers and keys never logged; access log (off by default) logs method, path without query, status, duration, request id only. |
| Panic in Rust | Task-local, 500/close, worker continues; a panic in the accept loop exits the worker non-zero. |
| Python exception escaping `pipeline` | Cannot by construction (`error_response` catches `Exception`); `BaseException` (cancellation) → connection gets 503/close. |

---

## 7. Test plan

* **Pure Rust (`crates/server`, runs on macOS and Linux):** limits table, timers (tokio
  `start_paused`), plan resolution, early-answer bytes, queue bound/wake-on-transition, shutdown
  ordering, smuggling raw-socket cases against an in-process server with a stub dispatcher.
* **Bindings (`cargo test` links only on Linux CI):** `PreparedRequest` lifetime, `respond` after
  receiver dropped, no GIL on tokio threads (a test that holds the GIL on the main thread while
  tokio serves native 404s must not stall).
* **Python parity suite (new, `tests/parity/`)**: one table of requests (methods, HEAD, 404/405
  with Allow, 413/414/431, CORS preflight + simple, host refusal, HTTPS redirect, trusted proxy
  headers, request-id trust rules, route vs policy headers, 401/403 ordering vs 413, 406, timeout
  504 vs handler TimeoutError, executor 503, not-ready 503, transaction rollback before error,
  debug bodies, docs/OpenAPI pages) executed against **ASGI (Granian), WSGI and Rust** over real
  sockets; asserts status, body bytes, header set (request-id value excepted). Marked `runtime`;
  Linux CI runs it (macOS local runs of Granian tests hit the known `PermissionError`).
* **Disconnect and shutdown:** client closes mid-handler (result discarded, no crash, behaviour
  compared to Granian and recorded), SIGTERM with in-flight requests (drain then lifespan
  shutdown, exit 0), second SIGTERM.
* **Process model:** `workers=2` spawn, supervisor has no tokio runtime, distinct request-id
  prefixes, startup failure exit code.
* **Dev reload (section 8.1):** file change → new worker serving within a bounded time; syntax
  error → worker exits, supervisor prints the traceback and waits for the next change, then
  serves again; tested with a temporary project on Linux and macOS.
* **Performance:** harness entries above; results JSON retained, report regenerated; CV recorded.
* **Coverage:** maintained or improved; no prose tests.
* **CI constraints:** Linux runners for bindings tests, Granian runtime tests and parity; macOS
  only for pure-Rust crate tests locally. Wheel job must build with the server feature on every
  published target (section 8.3).

---

## 8. Default server, `dev` reload, availability, dependencies

### 8.1 `hinoki dev` on the Rust server

* Reload = **Python supervisor + `watchfiles`** (same file-watching library Granian's `reload`
  extra uses): the supervisor watches the project (`*.py`, plus `settings` files; ignores
  `.venv`, `__pycache__`, `.git`), debounces 50-200 ms, sends SIGTERM to the single worker, waits
  for graceful shutdown (`shutdown_timeout`), spawns a new worker. The listening socket stays
  owned by the supervisor across restarts, so connections queue in the backlog instead of being
  refused (an improvement over a bind/unbind cycle; whether Granian keeps its socket across
  reloads is not verified and not claimed).
* Difference from Granian: same watcher, but Hinoki owns the supervisor, the restart message and
  the error output; no Granian process tree. `workers` is forced to 1 in dev (as today).
* Dev experience: `debug=True` default only via settings (unchanged); on startup failure or import
  error the supervisor prints the traceback once, prints "waiting for changes", and restarts on the
  next file change instead of exiting; a one-line access log is on by default in `dev` only.
* `watchfiles` missing → same error message as today.

### 8.2 Gate for flipping the default (build step F)

All of: R6 gate passed (D2 definition; R7 too only if the owner makes TLS part of the default, see D5) and their reviews closed; parity suite green on
ASGI/WSGI/Rust in Linux CI on the exact head; dev reload tests green on Linux and macOS; wheels
with the server built and smoke-tested (`hinoki serve` + one request) for every platform Hinoki
publishes; docs updated. R8/R9 are **not** required for the flip (whether Granian serves HTTP/2 under the current `serve.py` options, which set no HTTP mode, is unverified; if it does, it remains
available via `--server granian`). Until F merges, default = Granian.

### 8.3 Changes the flip needs

* `ServerSettings`: new field `server: Literal["rust", "granian"] = "rust"` (was implicit
  Granian); CLI `--server {rust,granian}` on `dev` and `serve` overrides it (`--server` added in
  R6 with default `granian`; F changes only the default).
* `serve.py`: split into `serve.py` (argument handling, dispatch) + `runtime/rustserver.py`
  (supervisor/worker) + existing Granian launcher function.
* `scaffold.py` (`hinoki new`): generated `pyproject` dependency stays `hinoki[server]` (watchfiles
  for `dev`); generated README text "serve with reload" unchanged in command, mention
  `--server granian` once.
* Docs: `docs/serving.md` (both servers, selection, limits, TLS later, dev reload),
  `docs/cli.md`, `docs/settings.md` (new fields, which server honours which),
  `docs/request-lifecycle.md` (boundary of 3.1), `docs/quick-start.md`, `docs/status.md`,
  `README.md` lines 47 and 324 ("runs as ASGI under Granian"; "WebSockets are not available"
  stays true until a public API exists), `ROADMAP.md` ("The ASGI adapter and application
  handlers run in Python" → precise wording for both servers), `python/hinoki/core.pyi`, new ADR
  0005 superseding ADR 0001's "no second server" and amending ADR 0002 (runtime shared by ORM and
  server). Benchmarks pages show both stacks.
* Examples: `examples/` commands unchanged (`hinoki dev`); add none.

### 8.4 When the Rust server is unavailable (D6 rationale)

Silent fallback would make "which server served this" ambiguous in benchmarks and bug reports;
an explicit request must therefore fail. A default that fails hard on a platform without the
server would break `hinoki dev` for users who only installed `hinoki[server]`; one warning plus
Granian keeps them working and tells them why. Detection: `hinoki.core.server_available()`
returns `(bool, reason)`; built with a Cargo feature `server` that is on by default for all
targets where hyper/tokio build (expected: all Tier-1 wheel targets; to verify in the wheel job).

### 8.5 `hinoki[server]` and Granian

After F, Granian is no longer needed for `serve`, but remains (a) the A/B reference in the test
and benchmark groups (`test` group keeps `granian[reload]`), (b) the fallback in 8.4, (c) the only
HTTP/2 path until R8. Recommendation (D7): keep `hinoki[server] = ["granian[reload]..."]` until
R8 passes, add `hinoki[reload] = ["watchfiles..."]` and make `hinoki[server]` include it; after R8
the owner may slim `hinoki[server]` to `watchfiles` only (Granian moves to an optional
`hinoki[granian]`). Core wheel size grows by hyper/h2/rustls code (rustls/ring already linked by
the ORM); size measured in the wheel job, not assumed.

---

## 9. Build steps, file ownership, dependencies

Numbered; one owner (agent or person) per file at a time; never two agents in one function.

1. **R5a Python pipeline** (Mid model from this design). New `python/hinoki/runtime/pipeline.py`;
   `runtime/asgi.py` delegates to it. Depends on R1 merged. Owner: one agent for both files.
   Verify: full pytest with PostgreSQL, ruff, mypy, complexipy, `profile_request.py` 5 vs 5.
2. **R5b crate skeleton** (`crates/server/{Cargo.toml,src/lib.rs,plan.rs,early.rs,limits.rs,
   queue.rs}`, workspace member, `#![forbid(unsafe_code)]`, hyper/hyper-util/http-body-util/bytes
   pinned). Owner: one Rust agent. Verify: `cargo clippy -D warnings`, `cargo test -p
   hinoki-server` (runs on macOS), rust-code-analysis.
3. **R5c bindings glue** `crates/bindings/src/server.rs` (`PreparedRequest`, `Plan` builder from
   Python bindings, `respond`, `server_available`), `lib.rs` registration, `core.pyi`. Depends on
   2. Owner: same Rust agent as 2 (serial). Strong-model independent review (GIL/lifetimes).
3b. **Native-await completion through the wake queue** (section 3.3 mitigation): ORM `bridge!` and the body awaitable complete on the loop thread via the queue + wake fd instead of `Python::attach` on tokio threads. Owner: the Rust agent of step 3 (serial; `db.rs` is shared). A/B on **both** Granian and the Rust server; independent Opus review (GIL, cancellation of pending DB futures). Adopted only if it does not regress Granian.
4. **R5 gate** measurement and report; owner decision.
5. **R6.0 spike** in a scratch worktree, not merged; numbers into handoff; owner decision.
6. **R6 server**: `crates/server/src/{conn.rs,accept.rs,shutdown.rs,timeouts.rs}`;
   `python/hinoki/runtime/rustserver.py` (worker loop, wake-fd reader, lifespan driving,
   supervisor); `serve.py` `--server`; `settings.py` new fields + refusal under Granian. Owners:
   Rust agent (crate), Python agent (runtime/rustserver.py, serve.py, settings.py), serial on
   `serve.py`. Then `tests/parity/`, raw-socket tests (separate test agent). Benchmarks:
   `server_floor.py`, `profile_request.py`, tenant `tenant_run.py`/`tenant_report.py` on
   `bench/tenant-comparative` (benchmark agent; that branch only). Independent Opus review
   (GIL, cancellation, shutdown, smuggling) → fix pass → full verification → A/B gate → report.
7. **ADR 0005** (server alongside Granian, shared runtime) and docs for `--server rust` as an
   option (`serving.md`, `cli.md`, `settings.md`, `request-lifecycle.md`), strict MkDocs build.
8. **R7 TLS**: `crates/server/src/tls.rs`, settings `tls_cert_file`/`tls_key_file`; review; gate.
9. **R8 HTTP/2**: `crates/server/src/h2.rs` limits; review; gate.
10. **R9**: upgrade refusal (parity test). WebSocket API design only if the owner approves D4's
    follow-up.
11. **F default flip** (section 8): `settings.py` default, `serve.py`, dev reload supervisor tests,
    `scaffold.py` text, `pyproject.toml` extras (D7), README, ROADMAP, docs; wheel job with the
    server feature on all targets. Only after 8.2 holds.
12. **R10** benchmark tables with "Hinoki · Granian" and "Hinoki · Rust server" (P1, P2, P4,
    minimal Rust-server floor in P3), from retained JSON, quiet host.

Dependencies to add (pinned at implementation time, not chosen blindly here): `hyper` 1.x
(`server`, `http1`, later `http2`), `hyper-util` (`tokio`, `server-graceful`), `http-body-util`,
`bytes`, later `tokio-rustls` (ring provider), possibly `tokio-tungstenite` (R9). Tokio features
added to the bindings crate: `net`, `io-util`, `signal` is **not** used (signals stay in Python).

---

## 10. Unknowns that must be measured or tested before any claim

* **GIL on tokio threads with the shared runtime** (3.3): today the ORM bridge's conversion takes the GIL on a tokio worker (`future_into_py` itself uses a blocking-pool thread); with 1 tokio thread this can stall the HTTP side of a worker. Size of the effect unknown until R6.0 runs a DB route with `runtime_threads` 1 and 2.
* Async handoff cost tokio → asyncio → tokio vs Granian's (R6.0). Whether the empty→non-empty wake
  batching actually batches under 64-connection load.
* Whether `eager_task_factory` helps or changes semantics for handlers (ordering of context vars).
* Granian's behaviour on client disconnect and at shutdown with in-flight requests (pin by test).
* Whether hyper's `header_read_timeout` covers keep-alive idle in the pinned version.
* `SO_REUSEPORT` balancing on macOS; accept distribution across spawned workers sharing one fd.
* One tokio worker thread (today's default) for both network and ORM: is it the bottleneck with
  several workers? Measure `runtime_threads` 1 vs 2.
* Wheel availability of the server on every target; wheel size growth.
* Linux numbers: none exist for any of this; macOS numbers are development numbers only.
