# ADR 0002 — Resources are owned per worker; ORM gets its own Tokio runtime

Status: Proposed · 2026-11-13

**Context.** ASGI lifespan runs per event loop; Granian runs lifespan once per worker (observed). Pools bound to a loop must not cross loops or fork.

**Decision.** A `WorkerRuntime` is created in lifespan startup and closed in shutdown. It owns a dedicated Tokio runtime (default 1 worker thread, configurable) for ORM and native I/O, bridged to the worker's running asyncio loop with `pyo3-async-runtimes`. Pools are created inside it. Nothing is opened at import, in factories, or before fork. Blocking `block_on` from a runtime/loop thread is forbidden.

**Consequences.** Each worker carries extra native threads (cost to be measured, V10). Pools are not shared between workers; the connection budget is `replicas × workers × pool_max`. Reusing Granian's internal runtime is rejected because it is not a public API.

**Alternatives.** Reuse Granian's runtime (private, brittle); Python driver such as asyncpg (contradicts the Rust ORM requirement).

**Revisit when** the spike (R7) shows the thread/latency cost is unacceptable or `--task-impl rust` offers a supported bridge.
