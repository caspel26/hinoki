# ADR 0001 — Own ASGI adapter on Granian; RSGI measured later

Status: Accepted · 2026-11-18 (proposed 2026-11-13)

**Context.** The product needs a Rust HTTP server without building on a Python web framework. Granian (Rust, hyper/tokio) serves ASGI and RSGI. ASGI has a standard lifespan; RSGI has `__rsgi_init__`/`__rsgi_del__`, which are not equivalent to lifespan. Measured: Granian 2.8.8 exits non-zero when ASGI lifespan startup fails (see RUNTIME-DESIGN §1.1).

**Decision.** Implement a small ASGI adapter (`hinoki.runtime.asgi`) and run it on Granian. Keep it thin so an RSGI adapter can be added as an alternative after measurement. No second HTTP server for the first release.

**Consequences.** Every request crosses Python at the ASGI boundary; the path is not described as core. Portable to other ASGI servers for testing. We take a dependency on Granian's CLI/option names, pinned and validated per version.

**Alternatives.** RSGI first (fewer allocations, but server-specific and different failure semantics, unmeasured); own hyper server (two servers to maintain, no evidence it is needed).

**Revisit when** a benchmark of ASGI vs RSGI on equivalent payloads exists.

## Amendment (2026-11-18)

Accepted. Since this ADR was proposed, Hinoki also ships a first-party Rust server selected with `--server kiso` or `server.server = "kiso"` (`docs/serving.md`). The Rust server is now the default server and Granian with the ASGI adapter remains selectable with `--server granian`, so "no second HTTP server" no longer holds for the first release. Requests the Rust server does not answer natively still cross Python, and no ASGI-versus-RSGI measurement exists yet, so the revisit condition above stands.

## Amendment (2026-11-22): RSGI measured, not adopted

Measured on Granian 2.8.8 (one worker, `/health`, bombardier with 100 connections, alternating rounds, indicative load): a minimal ASGI app reaches about 38k requests/s and a minimal RSGI app about 41k, but Hinoki on its real ASGI adapter reaches about 13.8k. A shim that feeds the same pipeline through RSGI with the same inputs as the ASGI adapter is only 1.8% faster, and the ASGI protocol itself costs about 1.75 µs of roughly 72 µs per request (about 2.4%). The framework's own work dominates, so an RSGI adapter is not worth a second code path.

RSGI also cannot match ASGI semantics: it exposes only the already decoded path, so `%2F`, `%25` and invalid UTF-8 cannot be routed as the ASGI adapter routes them. **Decision:** keep the ASGI adapter on Granian; revisit only if the framework's per-request cost falls far enough for the protocol share to exceed about 10%.
