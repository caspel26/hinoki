# Rust-first request path (design, for owner review)

Status: DRAFT, 2026-11-16. Nothing implemented. Local file (gitignored by `docs/*-DESIGN.md`).
Owner intent (2026-11-16): the project exists to use Rust as much as possible; the goal is to
beat Django Bolt. This design says where to cut and how to find out, before building.

## 1. Evidence (measured, with its limits)

All Apple M4 Pro, release extension, host NOT idle, cProfile inflates Python costs (locates, does
not quote). Sources: `benchmarks/README.md`, `benchmarks/results/request-profile-*`,
`docs/ADAPTER-PREPARED-PLAN-DESIGN.md`.

* `GET /health` (no DB), one Granian worker: minimal ASGI app 7.6-8.8 us/request, Hinoki
  20.9-23.7 us. Hinoki's layer is about 12-16 us, about two thirds of the request.
* In process, event-loop CPU of that layer: 11.5-13 us. cProfile split: Hinoki Python 48-51%
  (adapter: `_http`, `build_request`, `_call`, `_serve`, `_respond`, dispatch), Python builtins
  18-21%, asyncio 11-12%, other libraries 8-12%, **Rust calls 6-7%**, application 3%.
* Granian vs the Rust server (Codex, preliminary, one machine): no measurable difference
  (throughput +0.6..3.4%, p99 worse for Rust in all four scenarios). Swapping the HTTP server
  while the same Python pipeline runs behind it moves almost nothing.
* Earlier Rust moves (routing, JSON decode, percent-decoding) gave nothing end to end because
  each Python<->Rust crossing costs about as much as the work it saves (about 0.3 us per call).
* ADAPTER-PREPARED-PLAN stage 2 (fold three crossings into one) was measured as about 2% of a
  request and rejected. **That conclusion stands for shaving the Python adapter. This design does
  something different: it removes the Python pipeline for requests that do not need it, instead of
  making it cheaper.**
* Django Bolt (Actix + Rust routing, Python only for the handler) was ahead on `health`
  (about 63.5k vs 41k req/s, 2026-11-14, noisy, predates later work). Not re-measured; the
  reason is a hypothesis (less Python per request), not a finding.

Owner (2026-11-16): Hinoki must not become a copy of Django Bolt. Bolt is a performance reference in
benchmarks only; the design follows Hinoki's own architecture (Rust ORM, validation, auth, server).

Consequence: the remaining cost is the Python event-loop path per request (asyncio task, pipeline,
`Request` object, contextvars, timeout context, response assembly). Making the Rust parts faster
cannot help; only not running that Python path can.

## 2. Lanes

A request is served by the cheapest lane that preserves its behaviour. Selection is made once per
route at startup (never per request by guessing).

* **Lane A: Rust only.** Request in, response out, no GIL, no asyncio, no Python object.
  Eligible: (a) static/native responses (`/health`-like, fixed or computed in Rust); (b) generated
  `CrudAPI`/`Resource` actions whose configuration is purely declarative (see 4).
* **Lane B: Rust-driven Python handler.** Rust does match, parse, validate, auth (if native),
  policy headers; takes the GIL once, calls the user's handler with the already built values,
  drives the coroutine itself when it awaits only native awaitables (the ORM bridge from
  native-await, build step 3b), and writes the response head and body. Handlers that await
  anything else (asyncio sleep, httpx, locks) fall back to today's asyncio path automatically.
* **Lane C: today's pipeline.** Unchanged. Always correct, always available, the oracle.

Policy headers (security, CORS, request id, content type) and error-to-response translation move
to Rust for lanes A and B; lane C keeps its Python code. Both must produce byte-identical
responses (differential tests, section 7).

## 3. Step 0: spikes that decide whether lanes B and A are worth building

Each is a scratch measurement (worktree, not merged), same host session, five alternating runs,
quiet host, `server_floor.py`/`oha` method, run by Codex if the host here is not quiet.

* **S1, floor of lane A.** Rust server answers `/health` natively with no Python. Gives the upper
  bound for any Rust-only route and the ceiling vs Bolt.
* **S2, floor of lane B.** Rust server calls a trivial sync Python handler: one GIL acquisition, no
  asyncio, no pipeline. Gives the cost of "Rust does everything, Python only runs the handler".
* **S3, feasibility of driving a coroutine from Rust.** `coro.send(None)` loop for a handler that
  awaits only native awaitables (ORM bridge). Check: result, exceptions, `CancelledError` on client
  disconnect, contextvars, behaviour when the handler awaits a non-native awaitable (must be
  detected and handed to asyncio without double running).

Owner direction (2026-11-16): put as much as possible in Rust; this is the purpose of the
project. The spikes therefore do NOT decide whether to move work to Rust. They decide the ORDER
(biggest measured gain first) and which fallbacks are needed. If a spike shows a lane gains little,
it is still built after the lanes that gain more, and the numbers are recorded honestly (no speed
claim without retained measurements).

## 4. Lane A for generated CRUD (the part Bolt does not have)

Bolt stops at server and routing; its ORM is Django. Hinoki already has in Rust: query compiler,
Postgres driver, validation, JSON serialization, JWT. A generated action can avoid Python end to
end when nothing in its configuration is Python code:

Goal: as many generated actions as possible run in lane A. Python callbacks cannot run in Rust,
so the lever is to give each common callback a DECLARATIVE form that Rust executes, and keep lane C
only for arbitrary Python code:

* `queryset=` callable -> declarative filters/ordering (`filters=`, `order_by=`), Rust compiles them.
* `assign` callables and `identity_key` -> named sources (`caller.id`, `caller.user`, constants,
  `now`), evaluated in Rust.
* schema `validate` hooks and custom auth providers -> stay Python (lane C or lane B); a custom
  provider can be wrapped by lane B (Python resolves the caller once, Rust does the rest).
* `access`: built-in JWT/token providers and `permissions=[...]` native; custom Python predicates
  -> lane B.

Each new declarative form is public API: designed with the owner, documented, and given a Python
oracle. `hinoki routes` shows the lane of each route and why a route is not lane A.

Pieces needed (each its own step, each with a Python oracle and differential tests):
1. Rows to JSON directly in Rust (extends `ROWS-RUST-DESIGN.md` option A: `RowPlan`; here the
   plan also carries the read schema's serialization, so no Python model is built at all).
2. Native auth: Bearer JWT verify + permission check from claims in Rust (crypto already Rust);
   behaviour identical to `auth.py`/`access.py` for 401/403, scoped tokens, `owner` filtering.
3. Request binding for list/detail/create/update (query parse, path params, JSON body validation:
   already Rust) and pagination envelope.
4. Error mapping (422 shape, 404, 409, 403) and transactions for writes, identical to lane C.
5. OpenAPI/docs unchanged: generated from the same declarations.

## 5. Lane B details and risks

* One GIL acquisition per request, on a dedicated Python thread or the worker's loop thread
  (decision after S2: running handlers on the event-loop thread keeps asyncio-compatible
  semantics for fallbacks; a separate thread avoids loop contention). Open question, S2/S3 answer.
* Sync handlers keep the bounded executor (already a pipeline rule).
* Cancellation and timeouts are Python semantics: request timeout (504) and client disconnect
  must cancel the handler; a native driver needs its own deadline and cancel path. This is where
  earlier reviews found bugs; it gets its own review.
* Contextvars (caller, request id, db transaction context) must be set around the handler
  exactly as `pipeline.py` does; a mismatch is a security bug for caller identity.

## 6. Semantics that must not change

Client disconnect and cancellation mid-request, timeout 504 vs handler `TimeoutError`, saturated
executor 503, not-ready 503, HEAD without body, request-id trust rules, route response headers vs
policy headers, CORS preflight, HTTPS redirect and HSTS, size limits, 413/408 body rules,
error translation order (rollback before HTTP error), trusted-proxy handling, HTTP/2 behaviour.

## 7. Verification

* Differential: for every lane A/B route, the same request set through lane C and the lane,
  comparing status, headers (names, order where specified) and body bytes. A request corpus from
  the existing blog/tracker/notes examples plus generated edge cases (invalid input, wrong auth,
  expired token, scoped permission, owner mismatch, pagination edges).
* Existing suites run with a setting that forces lane C (oracle) and with lanes enabled.
* Benchmarks: S1/S2 first; then the comparative suite (Hinoki lanes vs Hinoki lane C vs Bolt), five
  alternating runs, quiet host, retained raw JSON, CV recorded. No claim without them.
* Independent Opus review for lane A auth and lane B concurrency/cancellation (security-sensitive).

## 8. Scope and rollout

* Only the Rust server gets lanes (`--server rust`). Granian keeps the Python pipeline. The
  performance story therefore ties to the Rust server; the default stays Granian until the final
  benchmark and the flip gate (owner decision pending on the preliminary verdict).
* A lane is opt-in per app first (`server.fast_path`, default off), then default on for the Rust
  server once the differential suite and reviews are clean.
* `hinoki routes` shows the lane of each route and why a route is lane C.

## 9. Unknowns, stated plainly

* Whether S2 clears the gate: unknown. If the 6-7% Rust share is the real floor of the crossing,
  lane B may only save the asyncio/pipeline part (about 30-40% of the layer, by profile), which is
  the gate's order of magnitude, not far above it.
* Whether lane A on CRUD beats Bolt on `list`/`detail`: unknown. On those routes PostgreSQL wait
  (about 260 us for `detail`) dominates; removing Python saves event-loop CPU, which raises
  throughput under load more than it lowers single-request latency.
* Bolt's numbers are old and noisy; a fair comparison needs the quiet-host run.

## 10. Order of work

0. S1, S2, S3 spikes and the gate (small, scratch). Record numbers in the handoff.
1. If lane B passes: coroutine driver + response head in Rust + fallback, with differential tests.
2. Lane A static routes (`/health`), then rows-to-JSON, native auth, CRUD list/detail, then writes.
3. Final benchmark by Codex on the whole handoff, retained JSON.

## 11. Owner decisions

Settled by the owner (2026-11-16): maximum work in Rust; the 30%/2x gate is an ordering aid, not a
go/no-go. Treated as accepted unless the owner objects: policy headers in Rust for lanes A and B
(lane C unchanged); lane A for CRUD grows through declarative forms (section 4), designed one at a
time with the owner. Spikes run here first for relative numbers (host not quiet); the final
benchmark stays with Codex at the end of the handoff.

## 12. Spike results (2026-11-16, scratch worktree `../hinoki-spike`, branch `spike/rust-path`)

Host NOT quiet (load 3.7-8.5, container running, load generator not pinned: macOS). One M4 Pro,
`GET /health` only, HTTP/1.1, no DB, 1 worker, oha 64 connections, 5 alternating rounds of 3x15 s,
medians of round medians, CV across rounds <= 1.2%. Relative numbers only. Floors, not equivalent
behaviour (S1/S2 skip classification, policy headers, request id, error translation).

| Stack | req/s | us/request (1e6/req/s) |
| --- | ---: | ---: |
| S1 Rust-only `/health` (no Python at all) | 116,561 | 8.58 |
| S2 Rust calls one trivial sync Python handler | 91,171 | 10.97 |
| A Hinoki `/health`, today's Rust server (full pipeline) | 43,254 | 23.12 |
| B Hinoki `/health`, Granian | 32,875 | 30.42 |
| C minimal ASGI on Granian | 104,760 | 9.55 |
| D actix-web 4.19.0, 1 worker | 127,265 | 7.86 |
| E hyper 1.12.0 + tokio 1.54.1, 1 worker thread | 130,912 | 7.64 |

Reading:
* S2 removes 12.2 us (53%) of today's Rust-server request: the gate for lane B (30%) is passed on
  this floor. Calling a trivial sync Python handler from Rust costs about 2.4 us over S1.
  NOT measured: GIL contention with a busy loop thread, real handler work, an async handler.
* S1 is 2.7x A, but only 1.28x S2: the 2x gate for lane A over lane B is NOT met on `health`.
  Lane A's value is on routes where the Python handler would do real work (CRUD with DB), which
  this spike does not measure.
* actix-web has no floor advantage over hyper here (hyper 2.9% faster, ranges do not overlap).
  S1 is 0.9 us above bare hyper: Hinoki's transport layer is cheap. Switching to Actix is not
  supported by data.
* Hinoki on today's Rust server is 1.35x Hinoki on Granian in this session (43.3k vs 32.9k),
  unlike Codex's preliminary run (no measurable difference, other app/DB/host). Do not quote either.

S3 (coroutine driving from Rust), findings:
* The ORM bridge awaitable is bound to a running asyncio loop (`create_future` at creation,
  `completion.rs submit`); `db.fetch` outside a loop raises RuntimeError. A Rust-driven coroutine
  needs a NEW loop-independent awaitable type that yields a marker; detection must be
  `yielded is MARKER` (an `isinstance(Future)` test cannot tell a native await from
  `asyncio.sleep(0.01)`).
* send/throw/close semantics work as needed (result via StopIteration, exception, CancelledError
  runs `finally`).
* DESIGN CONSEQUENCE: a coroutine already advanced by the Rust driver and suspended on an asyncio
  future cannot be handed to `asyncio.create_task` (RuntimeError). So the fallback to asyncio
  must be decided BEFORE the first `send`, or the handler restarted (unsafe with side effects).
  Lane B therefore needs a static eligibility decision (handler declared/inspected as native-only,
  e.g. an explicit marker or a restricted API) or a hybrid driver that owns a minimal loop for the
  whole request. This replaces the "automatic fallback" in section 2 and is the first open design
  question for lane B.
* Not verified: contextvars propagation, cancellation on a real client disconnect, a real
  Rust-side marker awaitable.
