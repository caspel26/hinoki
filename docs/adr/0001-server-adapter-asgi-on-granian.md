# ADR 0001 — Own ASGI adapter on Granian; RSGI measured later

Status: Accepted · 2026-11-18 (proposed 2026-11-13)

**Context.** The product needs a Rust HTTP server without building on a Python web framework. Granian (Rust, hyper/tokio) serves ASGI and RSGI. ASGI has a standard lifespan; RSGI has `__rsgi_init__`/`__rsgi_del__`, which are not equivalent to lifespan. Measured: Granian 2.8.8 exits non-zero when ASGI lifespan startup fails (see RUNTIME-DESIGN §1.1).

**Decision.** Implement a small ASGI adapter (`hinoki.runtime.asgi`) and run it on Granian. Keep it thin so an RSGI adapter can be added as an alternative after measurement. No second HTTP server for the first release.

**Consequences.** Every request crosses Python at the ASGI boundary; the path is not described as core. Portable to other ASGI servers for testing. We take a dependency on Granian's CLI/option names, pinned and validated per version.

**Alternatives.** RSGI first (fewer allocations, but server-specific and different failure semantics, unmeasured); own hyper server (two servers to maintain, no evidence it is needed).

**Revisit when** a benchmark of ASGI vs RSGI on equivalent payloads exists.

## Amendment (2026-11-18)

Accepted as the default server path. Since this ADR was proposed, Hinoki also ships a first-party Rust server selected with `--server rust` or `server.server = "rust"` (`docs/serving.md`). Granian and the ASGI adapter remain the default and are unchanged, so "no second HTTP server" no longer holds for the first release and is superseded by that opt-in server. Requests the Rust server does not answer natively still cross Python, and no ASGI-versus-RSGI measurement exists yet, so the revisit condition above stands.
