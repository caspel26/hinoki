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

Not available yet (do not report as passed): real-DB tests, generated-OpenAPI checks, wheel/packaging check, benchmarks.

## Decisions

* **Python ≥ 3.12**, dev interpreter 3.13 (`.python-version`), CI 3.12 + 3.13, abi3 wheels — see [ADR 0004](adr/0004-python-version-and-build-backend.md).
* **Ruff** is the only linter/formatter (`ruff check` with `--fix`, then `ruff format`), version pinned in `pyproject.toml` (`lint` group) and in `.pre-commit-config.yaml`; keep them equal when bumping. `framework-handoff/` is excluded.
* **mypy --strict** is the separate type check. No overlapping tools (no black, isort, flake8, pylint).
* **maturin** is the build backend (`python-source = "python"`, extension `hinoki._core`); uv rebuilds when Rust sources change (`tool.uv.cache-keys`).
* Complexity: Ruff `C901` ≤ 10 as the Python budget; the Shiori budget of 15 was a design target, not tied to this tooling. Rust gets clippy defaults for now.
* Dependency groups: `lint`, `typecheck`, `test`, aggregated by `dev`. Granian is a *test* dependency only until the adapter ships.
