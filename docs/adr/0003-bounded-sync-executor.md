# ADR 0003 — Bounded sync executor with fail-fast backpressure

Status: Proposed · 2026-11-13

**Context.** `def` handlers must not block async handlers; threads must be bounded; Python threads cannot be killed and do not give CPU parallelism.

**Decision.** One `ThreadPoolExecutor(sync_workers)` per worker plus an admission semaphore of `sync_workers + sync_queue`. Context is copied with `contextvars.copy_context()` on the loop thread. When admission fails the request returns 503 with `Retry-After`. Granian's thread options are not used for this. Sync ORM terminals run only on executor threads and raise in the event-loop thread.

**Consequences.** Memory and latency are bounded by configuration; overload is visible instead of queued forever. Orphaned threads after a timeout still occupy slots until they return; saturation makes the worker unready.

**Alternatives.** Unbounded `asyncio.to_thread` (no backpressure); run sync handlers on the loop (blocks everything); one process per sync request (too heavy).

**Revisit when** V5/V6 tests or load measurements show the defaults are wrong.
