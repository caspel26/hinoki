# Contributing to Hinoki

Hinoki is pre-alpha. Read [`AGENTS.md`](AGENTS.md) (the working rules, also for AI agents), [`ROADMAP.md`](ROADMAP.md) (public project status) and [`docs/TOOLCHAIN.md`](docs/TOOLCHAIN.md).

```sh
uv sync --locked
uv run --locked pre-commit install
uv run --locked ruff check --fix . && uv run --locked ruff format .
uv run --locked mypy && uv run --locked pytest --cov
cargo fmt --all --check && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace
```

* Non-trivial changes start with a design note in `docs/` (see `AGENTS.md`); decisions go in `docs/adr/`.
* Keep commits scoped (`feat(core):`, `docs(runtime):`, `build:`, `ci:`, `test:`) and update the relevant status document when project status changes.
* Don't claim a test, benchmark or feature that does not exist. Don't add Pydantic, Django, FastAPI or Starlette.
