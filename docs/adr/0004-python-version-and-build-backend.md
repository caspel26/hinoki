# ADR 0004 — Python ≥ 3.12, abi3 wheels, maturin backend

Status: Proposed · 2026-11-13

**Context.** The example syntax uses `StrEnum`, PEP 695-friendly generics and modern typing. A Rust extension multiplies the wheel matrix. uv needs a PEP 517 backend that builds mixed Python/Rust projects.

**Decision.** `requires-python = ">=3.12"`; development interpreter pinned to 3.13 in `.python-version`; CI matrix 3.12 and 3.13 (3.14 added once the lock resolves without pre-releases for the dev group). PyO3 builds with `abi3-py312` so one wheel per OS/arch serves all supported CPython versions. Build backend: **maturin** (`python-source = "python"`, extension `hinoki._core`), driven by uv. Free-threaded builds are not supported yet.

**Consequences.** 3.11 users are excluded; `abi3` forgoes a few PyO3 optimizations (to be measured if relevant). Rust toolchain is required to build from source.

**Alternatives.** scikit-build-core (CMake overhead, no PyO3 integration); setuptools-rust (less maintained ergonomics for uv/CI); per-version wheels (matrix growth).

**Revisit when** abi3 overhead is shown to matter or 3.11 support is requested.
