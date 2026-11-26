# Toolchain and CI

All Python commands run through **uv** with the lock enforced; Rust through **Cargo**. CI runs the same commands.

## Setup

```sh
uv sync --locked          # creates .venv, builds the Rust extension (maturin), installs dev groups
uv run --locked pre-commit install
```

Requires: uv ≥ 0.9, a stable Rust toolchain (rustup) with `rustfmt` and `clippy`.
The PyO3 extension build backend requires maturin ≥ 1.16.0 (`[build-system]` in
`pyproject.toml`); uv selects it from the isolated build-system requirement.

## Gates

| Gate | Command |
| --- | --- |
| Lock is current | `uv lock --check` |
| Lint (+ autofix) | `uv run --locked ruff check --fix .` |
| Format | `uv run --locked ruff format .` |
| Types + typing fixtures | `uv run --locked mypy` (checks `python/hinoki`, `tests` and `examples`) |
| Python cognitive complexity | `uv run --locked complexipy . --failed` |
| Rust cognitive complexity | Install `rust-code-analysis-cli` 0.0.25, then run `mkdir -p /tmp/hinoki-rust-complexity && rust-code-analysis-cli -m -p crates -O json --pr -o /tmp/hinoki-rust-complexity && python scripts/check_rust_complexity.py /tmp/hinoki-rust-complexity` |
| Python tests + coverage | `uv run --locked pytest --cov` (runs `tests` and the tests of `examples/notes`, `examples/tracker`, `examples/blog` and `examples/mailroom`) |
| Only the real-server tests | `uv run --locked pytest -m runtime` |
| Rust format / lints / tests | `cargo fmt --all --check` · `cargo clippy --workspace --all-targets -- -D warnings` · `cargo test --workspace` (see PyO3 setup below) |
| All hooks | `uv run --locked pre-commit run --all-files` |

### Measuring coverage

Coverage also measures child processes (`patch = ["subprocess"]` with `parallel` and `sigterm` in `[tool.coverage.run]`), so the supervisor, worker and `hinoki serve` tests count. CI runs two phases and merges them: the main suite with `pytest -n 4 --cov`, then the isolated server and process suites (`tests/runtime`, `tests/kisoservertest.py`, `tests/kisoclitest.py`, `tests/wsgitest.py`, `tests/ratelimitservertest.py`, `tests/kisometricstest.py`, `tests/kisoreloadtest.py`, `tests/ratelimitsharedtest.py` and `tests/adminsharedlimittest.py`) with `--cov --cov-append`. Locally:

```sh
uv run --locked pytest -n 4 --cov --cov-report=term-missing
uv run --locked coverage report --precision=2 --skip-covered -m
```

Without the database URLs below, the database-backed tests (most of the admin, parts of the ORM and the migrator) are skipped and their lines show as uncovered. A few unreachable guards carry `# pragma: no cover` with the reason next to it.

### Database tests

The binding tests embed Python. Select the interpreter explicitly and add its
shared-library directory when running the workspace suite locally:

```sh
PYTHON="$(uv run --locked python -c 'import sys; print(sys.executable)')"
PYTHON_LIBDIR="$("$PYTHON" -c 'import sysconfig; print(sysconfig.get_config_var("LIBDIR"))')"
PYO3_PYTHON="$PYTHON" \
  LD_LIBRARY_PATH="$PYTHON_LIBDIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  cargo test --workspace
```

CI sets these variables for the Rust job using its uv-managed Python 3.13.

PostgreSQL-dependent tests in `tests/dbtest.py`, `tests/transactiontest.py` and `crates/orm/tests/postgres.rs` need a **disposable** server and run only when `HINOKI_TEST_DATABASE_URL` is set; otherwise pytest reports them as *skipped* and Cargo prints `SKIPPED`. Migration conformance tests for MySQL and MariaDB use `HINOKI_TEST_MYSQL_URL` and `HINOKI_TEST_MARIADB_URL`; test each supported backend separately and read the skip summary. A run without the PostgreSQL URL does not verify PostgreSQL behavior. Locally:

```sh
docker run -d --name hinoki-test-pg -e POSTGRES_PASSWORD=hinoki -e POSTGRES_DB=hinoki \
  -p 127.0.0.1:54399:5432 --tmpfs /var/lib/postgresql/data postgres:17-alpine
export HINOKI_TEST_DATABASE_URL=postgres://postgres:hinoki@127.0.0.1:54399/hinoki
uv run --locked pytest
PYTHON="$(uv run --locked python -c 'import sys; print(sys.executable)')"
PYTHON_LIBDIR="$("$PYTHON" -c 'import sysconfig; print(sysconfig.get_config_var("LIBDIR"))')"
PYO3_PYTHON="$PYTHON" \
  LD_LIBRARY_PATH="$PYTHON_LIBDIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  cargo test --workspace
docker rm -f hinoki-test-pg
```

CI provides the same database as a service container. Never point the variable at a database with real data: tests create and drop tables.

The Python and Rust suites include ORM/model tests. PostgreSQL-dependent tests
run only with a disposable `HINOKI_TEST_DATABASE_URL`; migration conformance
tests for MySQL and MariaDB also use `HINOKI_TEST_MYSQL_URL` and
`HINOKI_TEST_MARIADB_URL`. CI provides disposable real-server services.
The native CRUD differential test build enables the private `sql-trace` Cargo
feature so it can compare SQL text, parameters and order for both request lanes;
ordinary wheels are built without that feature. Build it into the environment
with `uv run --no-sync --with "maturin>=1.9.4,<2" maturin develop --manifest-path crates/bindings/Cargo.toml --features sql-trace`.
Without it those tests skip; set `HINOKI_REQUIRE_SQL_TRACE=1` (CI does) to make
a missing trace build fail them instead. The CI Rust job also runs
`cargo clippy -p hinoki-bindings --all-targets --features sql-trace -- -D warnings`.
Generated OpenAPI is validated with `openapi-spec-validator` inside the Python
tests. The retained [benchmark report](https://hinoki.caspel.dev/dev/benchmarks.md) has reproducible
end-to-end measurements, but benchmark runs are separate from the default CI
test gate.

## Decisions

* **Python ≥ 3.12**, dev interpreter 3.13 (`.python-version`), CI 3.12, 3.13 and 3.14, abi3 wheels (smoke-tested in a clean 3.12 environment by the `wheel` CI job).
* **Ruff** is the only linter/formatter (`ruff check` with `--fix`, then `ruff format`), version pinned in `pyproject.toml` (`lint` group) and in `.pre-commit-config.yaml`; keep them equal when bumping.
* **mypy --strict** is the separate type check. No overlapping tools (no black, isort, flake8, pylint).
* **maturin** is the build backend (`python-source = "python"`, extension `hinoki.core`); uv rebuilds when Rust sources change (`tool.uv.cache-keys`).
* Cognitive complexity is capped at 15 for every Python function and Rust function/closure. CI runs pinned `complexipy` and `rust-code-analysis-cli`; test and development code are included. Refactor over-budget functions instead of suppressing analyzer findings.
* **Unsafe Rust is gated.** `kiso`, `hinoki-orm`, `hinoki-mail` and `hinoki-ratelimit` have `#![forbid(unsafe_code)]`; `hinoki-bindings` has `#![deny(unsafe_code)]`, so a new `unsafe` block fails the build until its function carries a narrow `#[allow(unsafe_code)]` and a `SAFETY:` comment stating why it is sound. Today there are three categories: the inherited-listener descriptor in `kiso.rs` (`OwnedFd::from_raw_fd`), the CPython FFI calls of the typed decoder, and two typed-decoder functions (`string` and `number`) that use `str::from_utf8_unchecked` on bytes checked to be ASCII.
* Dependency groups: `lint`, `typecheck`, `test`, aggregated by `dev`. Granian is both the optional runtime server extra and a test dependency for server integration coverage.

## Two environments, two responsibilities

* **uv** owns the project: `.venv`, `uv.lock`, the dependency groups and every tool declared in them (`ruff`, `mypy`, `pytest`, `pre-commit` itself). Run them with `uv run --locked ...`.
* **pre-commit** builds its own isolated environments for the hooks it fetches (e.g. `ruff-pre-commit`); these are *not* the project `.venv` and are not managed by uv. That is why the Ruff revision in `.pre-commit-config.yaml` must equal the `ruff==` pin in `pyproject.toml` (`uv.lock`).
* Hooks never rewrite `sources/`-style historical snapshots (excluded globally). CI never applies fixes: it fails if a hook would change a file. `pre-commit install` is a manual step in a Git checkout, never triggered by importing the framework.

## CI workflow hygiene

* Third-party actions in `.github/workflows/` are pinned to a full commit SHA with the released tag in a trailing comment. To update one, resolve the new tag to its commit (`gh api repos/OWNER/REPO/git/ref/tags/TAG`), replace the SHA and the comment together, and let the PR's CI run prove it.
* Each `Python X` job sets `UV_PYTHON` to its matrix version and asserts the interpreter before testing. Without it `uv run` follows `.python-version` and every job would test 3.13. Locally, run another version with `uv run --locked --python 3.14 pytest` (use `UV_PROJECT_ENVIRONMENT` to keep its environment apart).
* `ci.yml` checks out with `persist-credentials: false`: no CI job pushes. `deploy-docs.yml` keeps the token because it pushes to `gh-pages`.
* The `audit` job checks `Cargo.lock` (`cargo audit`, `cargo deny check`) and `uv.lock` (`uv export` of every group and extra, then `pip-audit --require-hashes --disable-pip`). Locally: `uv export --locked --all-groups --all-extras --no-emit-project --format requirements-txt -o /tmp/requirements.txt && uvx pip-audit==2.10.1 --require-hashes --disable-pip -r /tmp/requirements.txt`.
* A pull request that only changes `docs/`, `theme/`, `assets/`, `mkdocs.yml`, `LICENSE` or Markdown files in the repository root skips the work of the `Python X`, `rust`, `wheel` and `audit` jobs: the `changes` job lists the files through the API and, on any doubt, runs everything. The skipped jobs still report success, so the required checks need no change. `pre-commit` checks only the files of such a pull request and `docs` always runs. CodeQL and SonarCloud do not start for such a pull request.

## Native extension: editable install, rebuild, wheel

| Task | Command | Notes |
| --- | --- | --- |
| Editable install (first time) | `uv sync --locked` | maturin builds a debug extension into the project |
| Rebuild after changing Rust | `uv sync --locked` (or any `uv run`) | `tool.uv.cache-keys` watches `crates/**/*.rs` and `Cargo.toml`; uv reinstalls when they change. Observed: touching `router.rs` made `uv run` rebuild |
| Force a rebuild | `uv sync --locked --reinstall-package hinoki` | use if in doubt; syncing Python deps alone does not prove the extension is current |
| Release-profile editable build | `uv run --locked maturin develop --release` | needs `maturin` on PATH (`uvx maturin` works); the release profile uses fat LTO and one codegen unit, so a clean release build takes about 60 s instead of 35 s (measured on one Apple-silicon laptop) |
| Wheel | `uv build --wheel` | abi3 (`cp312-abi3`) wheel in `dist/`; built and import-tested by the `wheel` CI job |
| Release wheels | `release-build.yml` (tag `v*` or manual) | manylinux x86_64/aarch64 and macOS arm64/x86_64 wheels plus the sdist, smoke-tested with `scripts/smoke_wheel.py`; artifacts only, nothing is published ([Release checklist](https://hinoki.caspel.dev/dev/release.md)) |
| Rust workspace tests | `cargo test --workspace` with the PyO3 environment above | PyO3 unit tests link to the selected Python interpreter; PostgreSQL tests require `HINOKI_TEST_DATABASE_URL` |
