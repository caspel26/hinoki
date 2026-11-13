# Toolchain

All Python commands run through **uv** with the lock enforced; Rust through **Cargo**. CI runs the same commands.

## Setup

```sh
uv sync --locked          # creates .venv, builds the Rust extension (maturin), installs dev groups
uv run --locked pre-commit install
```

Requires: uv ≥ 0.9, a stable Rust toolchain (rustup) with `rustfmt` and `clippy`.

## Gates

| Gate | Command |
| --- | --- |
| Lock is current | `uv lock --check` |
| Lint (+ autofix) | `uv run --locked ruff check --fix .` |
| Format | `uv run --locked ruff format .` |
| Types + typing fixtures | `uv run --locked mypy` |
| Python tests + coverage | `uv run --locked pytest --cov` |
| Only the real-server tests | `uv run --locked pytest -m runtime` |
| Rust format / lints / tests | `cargo fmt --all --check` · `cargo clippy --workspace --all-targets -- -D warnings` · `cargo test --workspace` |
| All hooks | `uv run --locked pre-commit run --all-files` |

### Database tests

DB tests (`tests/dbtest.py`, `tests/transactiontest.py`, `crates/orm/tests/postgres.rs`) need a **disposable** PostgreSQL and run only when `HINOKI_TEST_DATABASE_URL` is set; otherwise pytest reports them as *skipped* and Cargo prints `SKIPPED` — a green run without the variable proves nothing about the database layer. Locally:

```sh
docker run -d --name hinoki-test-pg -e POSTGRES_PASSWORD=hinoki -e POSTGRES_DB=hinoki \
  -p 127.0.0.1:54399:5432 --tmpfs /var/lib/postgresql/data postgres:17-alpine
export HINOKI_TEST_DATABASE_URL=postgres://postgres:hinoki@127.0.0.1:54399/hinoki
uv run --locked pytest && cargo test --workspace
docker rm -f hinoki-test-pg
```

CI provides the same database as a service container. Never point the variable at a database with real data: tests create and drop tables.

Generated OpenAPI is validated with `openapi-spec-validator` inside the Python tests. Not available yet (do not report as passed): ORM/model tests, end-to-end benchmarks.

## Decisions

* **Python ≥ 3.12**, dev interpreter 3.13 (`.python-version`), CI 3.12, 3.13 and 3.14, abi3 wheels (smoke-tested in a clean 3.12 env by the `wheel` CI job) — see [ADR 0004](adr/0004-python-version-and-build-backend.md).
* **Ruff** is the only linter/formatter (`ruff check` with `--fix`, then `ruff format`), version pinned in `pyproject.toml` (`lint` group) and in `.pre-commit-config.yaml`; keep them equal when bumping. `framework-handoff/` is excluded.
* **mypy --strict** is the separate type check. No overlapping tools (no black, isort, flake8, pylint).
* **maturin** is the build backend (`python-source = "python"`, extension `hinoki.native`); uv rebuilds when Rust sources change (`tool.uv.cache-keys`).
* Complexity: Ruff `C901` ≤ 10 as the Python budget; the Shiori budget of 15 was a design target, not tied to this tooling. Rust gets clippy defaults for now.
* Dependency groups: `lint`, `typecheck`, `test`, aggregated by `dev`. Granian is a *test* dependency only until the adapter ships.

## Two environments, two responsibilities

* **uv** owns the project: `.venv`, `uv.lock`, the dependency groups and every tool declared in them (`ruff`, `mypy`, `pytest`, `pre-commit` itself). Run them with `uv run --locked ...`.
* **pre-commit** builds its own isolated environments for the hooks it fetches (e.g. `ruff-pre-commit`); these are *not* the project `.venv` and are not managed by uv. That is why the Ruff revision in `.pre-commit-config.yaml` must equal the `ruff==` pin in `pyproject.toml` (`uv.lock`).
* Hooks never rewrite `sources/`-style historical snapshots (excluded globally). CI never applies fixes: it fails if a hook would change a file. `pre-commit install` is a manual step in a Git checkout, never triggered by importing the framework.

## Native extension: editable install, rebuild, wheel

| Task | Command | Notes |
| --- | --- | --- |
| Editable install (first time) | `uv sync --locked` | maturin builds a debug extension into the project |
| Rebuild after changing Rust | `uv sync --locked` (or any `uv run`) | `tool.uv.cache-keys` watches `crates/**/*.rs` and `Cargo.toml`; uv reinstalls when they change. Observed: touching `router.rs` made `uv run` rebuild |
| Force a rebuild | `uv sync --locked --reinstall-package hinoki` | use if in doubt; syncing Python deps alone does not prove the extension is current |
| Release-profile editable build | `uv run --locked maturin develop --release` | needs `maturin` on PATH (`uvx maturin` works) |
| Wheel | `uv build --wheel` | abi3 (`cp312-abi3`) wheel in `dist/`; built and import-tested by the `wheel` CI job |
| Rust-only loop | `cargo test --workspace` | no Python needed |
