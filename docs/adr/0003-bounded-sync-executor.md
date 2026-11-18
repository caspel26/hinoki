# ADR 0003 — Bounded sync executor with fail-fast backpressure

Status: Accepted · 2026-11-18 (proposed 2026-11-13)

**Context.** `def` handlers must not block async handlers; threads must be bounded; Python threads cannot be killed and do not give CPU parallelism.

**Decision.** One `ThreadPoolExecutor(sync_workers)` per worker plus an admission semaphore of `sync_workers + sync_queue`. Context is copied with `contextvars.copy_context()` on the loop thread. When admission fails the request returns 503 with `Retry-After`. Granian's thread options are not used for this. Sync ORM terminals run only on executor threads and raise in the event-loop thread.

**Consequences.** Memory and latency are bounded by configuration; overload is visible instead of queued forever. Orphaned threads after a timeout still occupy slots until they return, so saturation stays visible until they finish.

**Alternatives.** Unbounded `asyncio.to_thread` (no backpressure); run sync handlers on the loop (blocks everything); one process per sync request (too heavy).

**Revisit when** a load test shows that the default `sync_workers` and `sync_queue` give the wrong balance between throughput and rejected requests.

## Implementation notes (2026-11-18)

Checked against the source (`runtime/executor.py`, `runtime/lifespan.py`, `blocking.py`):

- **Matches.** One `ThreadPoolExecutor(sync_workers)` per worker, admission of `sync_workers + sync_queue` calls, a slot released only when the thread returns, `503 overloaded` with `Retry-After` when admission fails, and sync ORM calls refused on the event-loop thread.
- **Corrected above.** Saturation does not make the worker unready: `Lifespan.ready` depends on a completed startup and on the managed critical tasks, so a saturated worker answers `503 overloaded` per request and stays ready. Marking it unready would remove it from the load balancer exactly when it is busy.

**Decided (2026-11-18):** readiness does not reflect saturation; a saturated worker answers `503 overloaded` per request and stays ready. The defaults are kept until a load test shows a better balance.
