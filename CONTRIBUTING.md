# Contributing to Hinoki

Hinoki is pre-alpha. Read [`AGENTS.md`](AGENTS.md) (the working rules, also for AI agents), [`ROADMAP.md`](ROADMAP.md) (public project status) and [`docs/toolchain.md`](docs/toolchain.md).

```sh
uv sync --locked
uv run --locked pre-commit install
uv run --locked ruff check --fix . && uv run --locked ruff format .
uv run --locked mypy && uv run --locked pytest --cov
cargo fmt --all --check && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace
```

* Discuss non-trivial changes in an issue before implementing them; record final architecture decisions as an ADR in `docs/adr/`.
* Name branches `<type>/<short-kebab-case-topic>` using `feat`, `fix`, `refactor`, `perf`, `test`, `docs`, `build`, `ci` or `chore` (for example, `feat/route-groups`). Delete merged branches.
* Split commits by cohesive scope (`feat(core):`, `test(core):`, `docs(runtime):`, `build:`, `ci:`); do not combine implementation, tests, docs, dependencies/tooling or repository policy in one catch-all commit.
* For user-facing code changes, include a minimal usage example in the pull request description and update the relevant guide/reference and checked-in example in the same work item. If no user-facing code is changed, mark the template section as not applicable and explain why.
* The docsite publishes merges to `main` as `/dev/`; only `vX.Y.Z` release tags update versioned release docs and the `stable`/`latest` aliases. See [documentation versions](docs/documentation-versions.md).
* Don't claim a test, benchmark or feature that does not exist. Don't add Pydantic, Django, FastAPI or Starlette.
