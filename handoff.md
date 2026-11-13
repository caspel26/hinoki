# Handoff — Hinoki

## START HERE

Updated 2026-11-13 (end of the first autonomous session). Project name: **Hinoki** (`hinoki` import, `HinokiAPI` application class, `API` for endpoint groups). The design package in `framework-handoff/` is **local only** (gitignored) and still says "Aptum": read it as Hinoki. Rules: `AGENTS.md`. Toolchain: `docs/TOOLCHAIN.md`. Designs: `docs/RUNTIME-DESIGN.md`, `docs/SCHEMA-DESIGN.md`, `docs/ORM-DESIGN.md`, ADRs in `docs/adr/`, typing findings in `docs/TYPING.md`, router benchmark in `benchmarks/`.

Code conventions from the owner (also in memory): **no underscores in file names** (tests use `*test.py`), flat simple code, no functions nested in functions, Sonar-style lint (Ruff rule families `PL, C90, ARG, ERA, RET, S, …`).

Brand: `assets/hinoki-identity-approved.png` unchanged; `hinoki-banner.png` / `hinoki-mark.png` are crops for the README.

## Queue

| ID | Work | State |
| --- | --- | --- |
| Q001 | Runtime design and ADRs | **Done**; V1–V8 verified (see RUNTIME-DESIGN §8). |
| Q002 | Typing and syntax proof | **Mostly done.** Decorators, `Access`, `API`, `Schema` proven with mypy strict; ORM probe shows `Post(...)` is checkable and `Model.acreate(**values)` is not. Pyright/IDE untested. |
| Q003 | Repo bootstrap and gates | **Done.** uv + maturin + Cargo workspace, Ruff pre-commit, CI with a PostgreSQL service, wheel smoke test, Python 3.12–3.14. |
| Q004 | Vertical slice blog | **Done**: runtime (R1–R5), schemas, body/query binding, OpenAPI, errors, auth, pagination, Rust DB engine, `transaction=True`, SQL compiler, Active Record, `examples/blog` (also under real Granian + PostgreSQL). Three independent reviews run; every reported finding fixed with a regression test. |
| Q005 | Relations and CRUD generation | **Implemented** (R2–R5: FK + explicit loading, versioned migrations, generated CRUD with `replaces`, sync model methods, typed `asave`). Relations review fixed (a5dd82c); Independent CRUD review (Opus-class, read-only) done: 1 high (subclass with a narrower resource kept the parent's write routes without the owner check), 2 medium, 6 low; all fixed with regression tests. M2M is the explicit join model with composite `unique=[(...)]` (model, migrations, tests); a `through=` sugar remains open. |
| Q008 | Request object, parsers, renderers (Rust) | **Owner-requested 2026-11-13, runs before Q006.** S1 design, **S2 done** (Python `Request`, request-first handlers), **S3 done** (Rust query/cookie/header parsing, JSON decode/encode; `benchmarks/requestbench.py`). **S4 done** (JSON/form/multipart parsers, `parsers=`, Rust multipart). S5 (renderers), S6, S7 (review) open. |
| Q006 | Rules and development MCP | Not started. |
| Q007 | Admin extra | Not started. |

## What exists (verified)

* `crates/bindings` (PyO3, `hinoki.native`): path router, DB bridge on asyncio (`Database`, `Transaction`, typed exceptions), `compile_query`.
* `crates/orm` (pure Rust): `tokio-postgres` + `deadpool` engine (leases that discard abandoned connections, poisoned transactions, aborted-commit detection, stale-plan recovery), query AST → SQL.
* `python/hinoki`: decorators/API/app, modules + lifespan, runtime (adapter, requests, responses, binding, executor, tasks), schemas + validation, auth, HTTP errors, pagination, OpenAPI, `db`, `orm`.
* `examples/blog`: reference app. `benchmarks/routerbench.py`: Python→Rust call cost (not end to end).
* Tests: 408 Python tests (97% coverage), 15 + 8 + 10 Rust tests, typing fixtures; DB tests need `HINOKI_TEST_DATABASE_URL` and are **skipped without it** (CI provides PostgreSQL).

## Commands (all green on 2026-11-13; last CI run success on 6 jobs)

```
uv lock --check
uv run --locked ruff check . ; uv run --locked ruff format --check .
uv run --locked mypy
HINOKI_TEST_DATABASE_URL=postgres://postgres:hinoki@127.0.0.1:54399/hinoki uv run --locked pytest --cov
HINOKI_TEST_DATABASE_URL=... cargo test --workspace ; cargo fmt --all --check ; cargo clippy --workspace --all-targets -- -D warnings
uv run --locked pre-commit run --all-files
uv build --wheel
```

The disposable database used locally was a Docker container `hinoki-test-pg` (command in `docs/TOOLCHAIN.md`); it was removed at the end of the session — recreate it to run DB tests. The other Postgres on the machine belongs to another project and was never touched.

## Decisions made (reversible, within the contracts)

Python ≥ 3.12/abi3/maturin (ADR 0004); ASGI on Granian first (0001); per-worker Tokio runtime via `pyo3-async-runtimes`, one thread by default (0002); bounded sync executor (0003); pure-Python schema engine behind a narrow interface; `tokio-postgres` + `deadpool`, PostgreSQL only; SQL generated in Rust from validated names; strict JSON validation without coercion; patterns documented anchored; 204 for empty responses; request deadline covers routing, auth, body and handler.

## Known limits / open items

* **Needs owner:** typing strategy for `Model.acreate/aupdate` (generated `.pyi` recommended); ASGI vs RSGI default; whether per-worker Tokio threads are acceptable (needs the end-to-end benchmark V10); JWT dependency choice (which crypto library); CHANGELOG policy; `hinoki` name availability on PyPI/crates.io (unchecked).
* Not implemented: relations, migrations, sync model methods, TLS to PostgreSQL, JWT, OpenAPI overrides/docs UI, cookies/headers binding, streaming, readiness flip before drain (needs a runner), `hinoki` CLI, rules engine, MCP, admin.
* Benchmarks: only the router micro-benchmark (`benchmarks/README.md`). No end-to-end numbers exist; no performance claim is made.
* Review status: runtime (review 1), auth/validation (review 2), database layer (review 3) were done by a separate read-only Opus-class agent; all findings fixed. Relations/CRUD/rules will need new reviews. Mapping of the Opus/Sonnet/Haiku split: this session was a Sonnet-class agent; review agents were Opus-class.
* Generated CRUD: `Resource.queryset` takes no principal, so per-user/tenant scoping of reads is not expressible (design gap from the CRUD review, not scheduled). Sync handlers abandoned by a deadline keep running to completion but can no longer hand work to the loop.
* Locking: `Cargo.lock` and `uv.lock` committed; dependency bumps must stay intentional.

## Next assignment

Order: ~~independent CRUD review~~ (done) → ~~M2M join model~~ (done) → Q008 S5–S7 → Q006 → Q007. Q008 detail below is superseded by `docs/REQUEST-DESIGN.md`.


Q005 step 1 (design first, `docs/RELATIONS-DESIGN.md`): FK and reverse relations with explicit loading (`select_related`/`prefetch_related`, no lazy I/O), then versioned migrations from model definitions (SQL inspectable, destructive operations flagged, never applied at startup), then generated CRUD with `replaces` and the policy hooks (ownership/field-assignment) proven against the blog. Keep the blog green throughout; add a wheel test for the DB bridge on 3.12.

## Git / deploy

Local commits scoped by area, pushed to `origin/main` under the owner's one-time permission (2026-11-13). Repository description and topics set via `gh`. No migrations, releases or deploys.
