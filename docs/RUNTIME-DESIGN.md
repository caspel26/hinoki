# Runtime design (Q001)

Status: **design, partly validated by experiment** · 2026-11-13 · Owner decisions are marked *Confirmed*; everything else is *Proposed* and is open to revision by evidence.

Scope: the Python / Rust / Granian boundary, ownership of event loop and pools, the bounded sync executor, lifespan, failed startup, cancellation and shutdown. It does not design the ORM, the schema engine, auth or admin.

Related ADRs: [0001](adr/0001-server-adapter-asgi-on-granian.md) · [0002](adr/0002-resource-ownership-per-worker.md) · [0003](adr/0003-bounded-sync-executor.md) · [0004](adr/0004-python-version-and-build-backend.md).

## 1. Observed facts

### 1.1 Measured here (Granian 2.8.8, Python 3.14, Linux, 2026-11-13)

A minimal hand-written ASGI app (`lifespan` + one slow route) run under `granian --interface asgi`:

| Experiment | Observed |
| --- | --- |
| Lifespan answers `lifespan.startup.failed` | Granian logs `ASGI lifespan startup failed`, shuts down and **exits with code 1**. No traffic is accepted. |
| `--workers 2` | Two worker processes (different PIDs) each ran their own `startup`; requests were served by both. Lifespan runs **once per worker**, not once per cluster. |
| `SIGTERM` to the supervisor while a 2 s request is in flight | The in-flight request **completed with 200**; the busy worker ran `lifespan.shutdown` only *after* that request finished; the idle worker shut down immediately; supervisor exited 0. |

These are single runs on a laptop, not a statistical result. They show behaviour of the protocol path, not latency.

The experiment script is reproduced as a real test in `tests/runtime/` once the adapter exists (see §8); until then it is evidence only.

### 1.2 Taken from `granian --help` (2.8.8)

Interfaces `asgi`, `asginl`, `rsgi`, `wsgi`; per-worker `--workers`, `--runtime-threads`, `--blocking-threads`, `--backpressure`; `--factory`; `--workers-kill-timeout`; `--respawn-failed-workers` (default off); `--loop` incl. `uvloop`/`rloop`; `--task-impl asyncio|rust`. These are the options the settings adapter may map; each is validated against the pinned Granian version.

### 1.3 Not yet verified (do not rely on)

Behaviour of `rsgi` init/del hooks under failure; ASGI throughput vs RSGI; interaction of `--task-impl rust` with our native futures; whether a second Tokio runtime inside a Granian worker has measurable cost; behaviour with `uvloop`/`rloop`.

## 2. Boundary: what is Python, what is Rust

```
 client ── TCP/TLS/HTTP ──▶ Granian (Rust: hyper/tokio)               [infrastructure]
                              │  ASGI scope/receive/send  ← Python objects cross here
                              ▼
                  hinoki.runtime.asgi  (Python, thin, ~one function)
                              │  method, raw path, headers, body bytes
                              ▼
                  hinoki._core (Rust, PyO3)                          [core]
                     router match · limits · param extraction · (later) body decode
                              │  matched action id + typed args
                              ▼
                  registry → pipeline (Python): auth → tx → handler → output validation
                              │                                    ▲
                              ▼                                    │ awaitable, loop-bound
                  hinoki._core ORM (Rust: query AST → SQL → pool → rows)
```

Honest accounting for the first release:

* **Rust:** routing, request limits, parameter extraction, response framing helpers, ORM query compilation/execution/pool/transactions.
* **Python:** the ASGI entry (one coroutine), the policy pipeline, user handlers, schema declaration, lifespan coordination, error translation.
* **Crossing cost:** with ASGI every request crosses Python at least twice (Granian→app, app→Rust router). We do **not** call this path native. We measure it before claiming anything (benchmark entry point is a Q004 deliverable).
* RSGI would remove the ASGI scope dict building, but is Granian-specific and has different init/shutdown hooks. **Proposed:** ship ASGI first (portable, standard lifespan), keep the adapter interface small enough that an RSGI adapter is a second file, decide on measurements.

### Native-request core: what lives in `hinoki._core` first

Phase 0 only proves the toolchain (`core_version()`). The first real Rust unit is the **router** (Q004): compiled once at `app.include`, immutable, shared read-only across threads; match is `(method, path) → ActionId + path params | NotFound | MethodNotAllowed`. No I/O, so it needs no runtime and can be tested in plain `cargo test`.

## 3. Ownership of loop, runtime and pools

| Resource | Owner | Created | Closed |
| --- | --- | --- | --- |
| Process supervision, signals, sockets | Granian | server start | server stop |
| Python event loop | Granian worker | worker start | worker stop |
| Tokio runtime for ORM/HTTP client (*proposed*) | `hinoki.runtime.WorkerRuntime`, one per worker process | in lifespan **startup**, never at import, never before fork | in lifespan **shutdown**, after modules close |
| DB pool | the ORM runtime, bound to that worker | after runtime, lazily on first use or in startup if `required` | before the runtime |
| Module resources (`ctx.provide`) | the module's `lifespan` context manager | startup, dependency order | reverse order |
| Sync executor | `WorkerRuntime` | startup | after drain |
| Task groups | the module/app that started them | `ctx.tasks` | cancelled and awaited at shutdown |

Rules (*Confirmed* by the approved constraints, restated as checks):

1. No I/O, thread, pool or loop at import or in the app factory.
2. Nothing opened before the worker exists; with N workers there are N runtimes and N pools. Connection budget = `replicas × workers × pool_max` (+ jobs, + admin).
3. An async resource lives on exactly one loop; never shared across loops or workers.
4. No global mutable request context. Per-request state is a `ContextVar` set by `RequestContext` and copied explicitly when crossing to threads.

### The second-runtime question (explicit cost)

*Proposed default:* the ORM owns a **dedicated multi-thread Tokio runtime per worker** (default `worker_threads = 1`, configurable, so each worker adds roughly its pool-driver thread plus Tokio's IO/timer needs), bridged to asyncio with `pyo3-async-runtimes` (`future_into_py` on the *running* loop). Alternative rejected for now: reuse Granian's internal runtime — not a public API, couples us to a private detail. The extra thread count is a known cost; whether it matters is a measurement, listed in §8.

Hard rule: the bridge never calls `run_until_complete`/`block_on` from inside a running loop; sync ORM terminals use a separate blocking path (§4) and are refused inside the event loop thread.

## 4. Sync handlers: bounded executor

*Proposed* (ADR 0003): per worker one `ThreadPoolExecutor(max_workers=execution.sync_workers)` guarded by an `asyncio.Semaphore(sync_workers + sync_queue)`.

* A `def` handler is dispatched with `loop.run_in_executor(pool, ctx.run, handler, …)` where `ctx = contextvars.copy_context()` taken on the loop thread.
* When the semaphore is exhausted the request fails fast with **503 + `Retry-After`** (bounded queue ⇒ backpressure, never unbounded growth). The wait is never unbounded either: `execution.request_timeout` applies from admission.
* Sync executor threads are an application limit, **separate** from Granian's `--runtime-threads` and `--blocking-threads`. We do not configure them through Granian.
* Sync ORM terminals (`Post.get`) execute on the same executor thread by blocking on the Rust runtime handle (`Handle::block_on`), which is legal there because the thread is not a runtime thread. Called on the loop thread → raises `BlockingCallInEventLoop` (rule-checkable).
* Python threads do not give CPU parallelism (GIL). CPU-heavy work belongs in a separate process/worker (documented limit, not solved here).
* A running thread cannot be killed. On deadline we stop awaiting it, mark the worker unready if the pool is saturated by orphaned work, and rely on the supervisor (`--workers-kill-timeout`) as the last resort.

## 5. Lifespan

Application is declarative; `Application.lifespan()` is the only place resources open.

```
startup  (per worker, on the worker loop, deadline = lifecycle.startup_timeout)
  1. validate settings + module graph          (already done at factory time; fail = process exit)
  2. open WorkerRuntime (Tokio, executor)
  3. AsyncExitStack: for module in topological(requires):
         await stack.enter_async_context(module.lifespan(ctx))
  4. mark ready  → send lifespan.startup.complete

shutdown (deadline = lifecycle.shutdown_timeout)
  1. mark not-ready (readiness probe false) — already true if SIGTERM arrived earlier
  2. cancel managed task groups, await them
  3. unwind the AsyncExitStack in reverse order (every exit attempted; errors collected)
  4. close pools, then the Tokio runtime, then the executor
  5. send lifespan.shutdown.complete
```

### Failed startup

If module *k* fails, the `AsyncExitStack` closes modules `k-1 … 1` (only those actually entered), then the runtime. The adapter then sends `lifespan.startup.failed` with a message that contains the module name and the exception type and **no settings values**. Observed behaviour (§1.1): Granian exits non-zero and never serves; therefore readiness is never true. We never answer "lifespan unsupported" to hide an error.

Startup timeout expiry is treated as failure with the same path; a wedged module `__aenter__` is cancelled, and if it ignores cancellation the supervisor kill timeout applies.

### Shutdown ordering, honestly

ASGI delivers `lifespan.shutdown` only after the server has finished handling active connections (observed §1.1: the in-flight request finished first). Consequences:

* *Drain* of in-flight requests is performed by **Granian**; our code cannot extend or shorten it from the lifespan hook.
* A *readiness flip before drain* (so a load balancer stops routing) cannot come from the shutdown hook. *Proposed:* the runner (`hinoki serve`) installs a SIGTERM handler in the supervisor that flips a readiness file/flag and then forwards the signal after `lifecycle.drain_grace`. Until the CLI exists, deployments rely on the orchestrator's preStop delay. This is a design item, **not implemented or measured**.
* Cleanup is idempotent; each step has its own deadline; errors are logged once each and do not stop later steps.

## 6. Cancellation, timeouts, transactions

* **Request timeout** wraps the whole action in `asyncio.timeout(execution.request_timeout)`. A client disconnect surfaces as `CancelledError` at the next `await`.
* **Rust futures** exposed through the bridge are dropped when their asyncio task is cancelled; Tokio/sqlx then abort the statement and the connection is discarded, not returned to the pool, if its state is unknown. *Proposed contract; to be proven by the "timeout during query" test.*
* **Transactions** (`transaction=True`): order is auth → open tx → handler → output validation → commit → response. Any exception, timeout or cancellation → rollback **before** HTTP error translation. A failed commit/rollback is reported as an error, never as success. The tx handle lives in a `ContextVar`; child tasks cannot reuse it concurrently (they get an error, not a shared connection).
* **Streaming** responses never keep a transaction open (separate contract, out of scope here).
* **Post-commit** side effects are callbacks run after commit; no rollback can recall an email already sent (documented, Q004).

## 7. Settings mapping (proposed)

`ServerSettings(backend='granian', interface='asgi', workers=1, runtime_threads=1)` ↦ `--workers`, `--runtime-threads`; `ExecutionSettings(sync_workers=8, sync_queue=64, request_timeout=30)` is ours; `LifecycleSettings(startup_timeout, drain_timeout, shutdown_timeout)` maps `drain/shutdown` to `--workers-kill-timeout` + our step deadlines. Defaults are *examples*, to be sized by measurement. Default deployment advice: one worker per replica, scale out externally; multi-worker is supported and explicit.

## 8. Verification plan (acceptance criteria — none of these are implemented yet unless stated)

| # | Check | Status |
| --- | --- | --- |
| V1 | Startup failure → process exits ≠ 0, never ready | **observed** with a hand-written ASGI app (§1.1); to become a test against the real adapter |
| V2 | Two workers → separate lifespans, separate pools | **observed** (PIDs, §1.1); pool isolation untested |
| V3 | SIGTERM during request → request completes, then shutdown | **observed** (§1.1); deadline/forced-kill path untested |
| V4 | Failure in module *k* closes 1…k-1 in reverse | pure-Python test with the coordinator (Q004 slice) |
| V5 | Sync handler that sleeps does not block concurrent async requests | test with adapter + executor |
| V6 | Executor full → 503 within bound, queue never exceeds `sync_queue` | test |
| V7 | Timeout during DB query → rollback, connection not reused | needs ORM runtime |
| V8 | Task group cancelled and awaited at shutdown | test |
| V9 | Reload leaves no residual resources | `hinoki dev` smoke test |
| V10 | Cost of Python↔Rust crossing, extra Tokio threads per worker | benchmark, retained baseline |

## 9. Implementation steps (small, with file ownership)

| Step | Deliverable | Owns | Depends on |
| --- | --- | --- | --- |
| R1 | `Module`, module graph (topo sort, cycle/missing/duplicate errors) | `python/hinoki/modules.py` | — |
| R2 | Lifespan coordinator with `AsyncExitStack`, reverse cleanup, deadlines | `python/hinoki/runtime/lifespan.py` | R1 |
| R3 | ASGI adapter (`lifespan` + `http`), failure messaging | `python/hinoki/runtime/asgi.py` | R2 |
| R4 | Bounded sync executor + context copy | `python/hinoki/runtime/executor.py` | — |
| R5 | Tests V1–V6, V8 using Granian as a dev dependency | `tests/runtime/` | R2–R4 |
| R6 | Rust router + benchmark skeleton (V10) | `crates/http/`, `benchmarks/` | Q003 |
| R7 | ORM runtime spike (Tokio + one query on disposable PostgreSQL) | `crates/orm/` | R3, Q004 |

## 10. Decisions

**Confirmed (owner):** Rust core for requests/routing/ORM; Granian first candidate; explicit lifespan, bounded sync execution, cancellation and shutdown; no Django/FastAPI/Starlette/Pydantic.

**Proposed here (reversible):** ASGI adapter first, RSGI measured later (ADR 0001); per-worker Tokio runtime + `pyo3-async-runtimes` bridge (ADR 0002); bounded executor with 503 backpressure (ADR 0003); Python ≥ 3.12 and maturin (ADR 0004); readiness flip via the runner (§5).

**Open, need evidence or owner:** ASGI vs RSGI default; whether per-worker extra Tokio threads are acceptable; exact drain choreography across Granian versions; PostgreSQL driver choice (SQLx vs tokio-postgres) at R7; free-threaded Python support.
