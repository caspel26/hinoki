# ADR 0002 — Resources are owned per worker; ORM gets its own Tokio runtime

Status: Proposed · 2026-11-13

**Context.** ASGI lifespan runs per event loop; Granian runs lifespan once per worker (observed). Pools bound to a loop must not cross loops or fork.

**Decision.** A `WorkerRuntime` is created in lifespan startup and closed in shutdown. It owns a dedicated Tokio runtime (default 1 worker thread, configurable) for ORM and native I/O, bridged to the worker's running asyncio loop with `pyo3-async-runtimes`. Pools are created inside it. Nothing is opened at import, in factories, or before fork. Blocking `block_on` from a runtime/loop thread is forbidden.

**Consequences.** Each worker carries extra native threads (cost to be measured, V10). Pools are not shared between workers; the connection budget is `replicas × workers × pool_max`. Reusing Granian's internal runtime is rejected because it is not a public API.

**Alternatives.** Reuse Granian's runtime (private, brittle); Python driver such as asyncpg (contradicts the Rust ORM requirement).

**Revisit when** the extra native threads per worker are measured to cost more than the isolation is worth, or when Granian offers a documented way to share its runtime.

## Implementation notes (2026-11-18)

The ADR is still Proposed. What the code does today, checked against the source:

- **Matches the decision.** Database pools are created in the worker's lifespan (`DatabaseModule`), closed in shutdown, never at import, in a factory or before fork, and are not shared between workers. The native server keeps a non-owning handle to the worker's pool and drains its own jobs before Python closes it.
- **Differs from the decision.** There is no `WorkerRuntime` object and no shutdown step for the runtime. The Tokio runtime used by the ORM bridge is process-wide and created on the first native database call (`crates/bindings/src/db.rs`, `ensure_runtime`). Because each worker is its own process, this is one runtime per worker in practice. Its thread count is `server.runtime_threads` (default 1), applied by `configure_runtime` when `hinoki serve` or the Rust server starts, and valid only before that first call.

**For the owner to decide**, not implemented here:

1. *Revise the ADR to the implemented design*: a lazily created, process-wide runtime, one per worker process, sized by `server.runtime_threads`. This is the smaller change and matches how workers are started.
2. *Change the code to the ADR*: a `WorkerRuntime` created in lifespan startup and closed in shutdown. It adds lifecycle code for a property the process boundary already gives.
