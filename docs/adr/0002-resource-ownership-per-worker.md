# ADR 0002 — Resources are owned per worker; ORM gets its own Tokio runtime

Status: Accepted · 2026-11-18 (proposed 2026-11-13)

**Context.** ASGI lifespan runs per event loop; Granian runs lifespan once per worker (observed). Pools bound to a loop must not cross loops or fork.

**Decision.** Each worker process owns one Tokio runtime for the ORM and native I/O, created lazily on the first native database call and sized by `server.runtime_threads` (default 1). It is bridged to the worker's running asyncio loop with `pyo3-async-runtimes`. Pools are created in the worker's lifespan and closed in its shutdown. Nothing is opened at import, in factories, or before fork. Blocking `block_on` from a runtime/loop thread is forbidden.

**Consequences.** Each worker carries extra native threads (cost to be measured, V10). Pools are not shared between workers; the connection budget is `replicas × workers × pool_max`, and `(workers + 1) × pool_max` per replica while a [reload on `SIGHUP`](../kiso.md#reload-on-sighup) runs one extra worker. Reusing Granian's internal runtime is rejected because it is not a public API.

**Alternatives.** Reuse Granian's runtime (private, brittle); Python driver such as asyncpg (contradicts the Rust ORM requirement).

**Revisit when** the extra native threads per worker are measured to cost more than the isolation is worth, or when Granian offers a documented way to share its runtime.

## Implementation notes (2026-11-18)

Accepted after checking the decision against the source. The decision was revised to the implemented design: there is no `WorkerRuntime` object; the runtime is process-wide, created lazily (`crates/bindings/src/db.rs`, `ensure_runtime`) and, because each worker is its own process, is one runtime per worker. Pools are created in the worker's lifespan (`DatabaseModule`), closed in shutdown, never at import, in a factory or before fork, and are not shared between workers. The native server keeps a non-owning handle to the worker's pool and drains its own jobs before Python closes it.

Under the Rust server `configure_runtime` applies `server.runtime_threads` before the first native call. On the Granian path the setting is passed to Granian instead and the ORM runtime keeps its default of one thread.
