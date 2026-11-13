# Handoff — Hinoki

## START HERE

Updated 2026-11-13. Project name: **Hinoki** (`hinoki` import/CLI, `HinokiAPI` application class, `API` for endpoint groups). The design package in `framework-handoff/` is **local only** (gitignored) and still says "Aptum": read it as Hinoki. Rules: `AGENTS.md`. Toolchain: `docs/TOOLCHAIN.md`. Runtime: `docs/RUNTIME-DESIGN.md` + `docs/adr/`. Typing: `docs/TYPING.md`.

Brand: `assets/hinoki-identity-approved.png` is the approved identity (unchanged); `hinoki-banner.png` and `hinoki-mark.png` are crops of it for the README.

## Queue

| ID | Work | State |
| --- | --- | --- |
| Q001 | Runtime design and ADRs | **Done (design)**. Granian behaviour for startup failure, two workers, SIGTERM drain was observed and is covered by tests; everything else in the design is proposed. |
| Q002 | Typing and syntax proof | **Partial.** Decorators, `Access`, `API`, `HinokiAPI` proven with mypy strict (positive + negative fixtures). ORM probe shows `Post(...)` is checkable and `acreate(**kwargs)` is not. `Schema`, `Principal[T]`, `Page[T]`, filters, pyright/IDE not covered. |
| Q003 | Repo bootstrap and gates | **Done.** uv + maturin + Cargo workspace, Ruff pre-commit, CI, lock enforced. No benchmark, DB, OpenAPI or packaging gate yet (wheel builds locally with `uv build --wheel`, not gated in CI). |
| Q004 | Vertical slice blog | Next. Start with runtime steps R1–R5 in `docs/RUNTIME-DESIGN.md` §9, then schema/ORM. |
| Q005–Q007 | Relations/CRUD, rules/MCP, admin | Not started. |

## What exists (verified)

* `crates/bindings`: Rust path router (`router.rs`) + PyO3 module `hinoki._core` (`Router`, `core_version`). 6 Rust tests.
* `python/hinoki`: `get/post/put/patch/delete`, `Access`, `API`, `HinokiAPI` (`include`, `resolve`, `routes`), `RegistrationError`. No server, no request handling, no schemas, no ORM.
* `tests/`: 12 Python tests (98% coverage of `python/hinoki`), typing fixtures, 3 real-Granian lifespan tests.

## Commands (all run and green on 2026-11-13)

```
uv lock --check
uv run --locked ruff check . ; uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest --cov
cargo fmt --all --check ; cargo clippy --workspace --all-targets -- -D warnings ; cargo test --workspace
uv run --locked pre-commit run --all-files
```

Environment used: uv 0.13.8, Rust 1.100.0 (rustup, user-local), Python 3.13.18 (uv-managed), Granian 2.8.8, PyO3 0.30.1, Ruff 0.15.12. CI run 43350663850 on `main` passed (rust, Python 3.12, Python 3.13, pre-commit).

## Decisions made this session (reversible, within the contracts)

Python ≥ 3.12 with abi3 and maturin (ADR 0004); ASGI adapter on Granian first, RSGI measured later (ADR 0001); per-worker Tokio runtime for the ORM (ADR 0002); bounded sync executor with 503 backpressure (ADR 0003); mypy strict as the separate type checker; Ruff `C901` ≤ 10 as the Python complexity gate; Granian only a test dependency for now.

## Open items / needs owner

* Shape of `acreate`/`aupdate` typing: generated `.pyi` stubs (recommended) vs `Post(...).asave()` vs plugin — `docs/TYPING.md`.
* ASGI vs RSGI default; acceptable extra Tokio threads per worker (needs benchmark V10).
* Readiness flip before drain needs a runner-level SIGTERM handler (not designed in detail).
* Name availability of `hinoki` on PyPI/crates.io not checked. CHANGELOG policy still open. PostgreSQL driver (SQLx vs tokio-postgres) at R7.
* Verified-but-not-reviewed: no independent code review was run (nothing here touches data/auth/migrations; concurrency is design-only). A review is required before R2–R5 land. Opus/Sonnet/Haiku mapping from `AGENTS.md`: this session ran as a single Sonnet-class agent; no delegated agents.

## Next assignment

Q004 step R1–R2: `Module` + module graph (missing/cycle/duplicate errors) and the lifespan coordinator (`AsyncExitStack`, reverse cleanup, per-step deadlines) in pure Python with tests V4/V8, then R3 ASGI adapter + V1–V3/V5/V6 against it. Add a wheel-build job and a Python 3.14 CI entry when ready.

## Git / deploy

Local commits scoped by area; pushed to `origin/main` on explicit owner permission given 2026-11-13 (one-time). No migrations, releases or deploys.
