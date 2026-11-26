# Working rules for Hinoki

Rules for anyone changing this repository, people and coding agents alike. This file is the single maintained rule set; `AGENTS.md` and `GEMINI.md` are symbolic links to it, so every tool reads the same text. Read [`ROADMAP.md`](ROADMAP.md) for project status, [`CONTRIBUTING.md`](CONTRIBUTING.md) for the workflow and [`docs/adr/`](docs/adr/) for architecture decisions that are already made. Do not reconstruct intent from chat history.

Do not state that a feature, benchmark number, supported version or check exists unless it does. If a command is not documented, report it as unavailable.

## Toolchain

Python runs through `uv` with the lock enforced, Rust through Cargo, with maturin as the build backend. See [`docs/toolchain.md`](docs/toolchain.md) for setup and the database-backed tests.

```sh
uv sync --locked
uv run --locked ruff check --fix . && uv run --locked ruff format .
uv run --locked mypy
uv run --locked complexipy . --failed
uv run --locked pytest --cov
cargo fmt --all --check && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace
uv run --locked --group docs mkdocs build --strict
```

## Product boundaries

- Independent Python framework: not built on another web framework. Infrastructure servers such as Granian are allowed.
- Substantial Rust core for request processing, routing and ORM execution. Document the real server/binding boundary; do not call a path entirely native if it crosses Python objects.
- Hinoki owns its public schema and settings interfaces. Do not add an external validation framework as a convenience dependency (no Pydantic, Django, FastAPI or Starlette).
- Base syntax: `@http.get`, `@http.post`, `@http.put`, `@http.patch`, `@http.delete`, `@http.query` (`from hinoki import http`), reusable typed `Access` policies, and `access` and `atomic` options on the decorator. Preserve normal Python and decorator composition. Do not reintroduce declaration blocks such as `with get/post`, or inline `get(query=...)`/`lookup=...` assignments.
- CRUD generation is required; its advanced syntax is provisional.
- Explicit application modules and dependencies, a defined worker lifespan, bounded sync execution, startup and graceful shutdown.
- Remote services do not share ORM joins or pretend to share local transactions.
- The admin (`hinoki.admin`) ships in `hinoki-api` with explicit mounting and migrations. Installing or importing Hinoki never mutates a database.
- Vocabulary: `Schema` (code font) is the Hinoki class; use "table state"/"migration state", "OpenAPI document" and "JSON Schema" for the other meanings ([`docs/glossary.md`](docs/glossary.md)).

## Code conventions

- Annotate public function signatures and class attributes. Decorators preserve signatures with `ParamSpec`/`TypeVar` and `functools.wraps`.
- Imports go at the top of Python files; a local import is allowed to avoid a circular dependency, with a comment saying so.
- Every Python function and Rust function or closure has cognitive complexity of 15 or less (`complexipy`, `rust-code-analysis-cli`). Refactor instead of suppressing.
- Explicit resource ownership: no import-time I/O, no connection pool created per worker after process start, no async pool shared across event loops, no global mutable request context.
- Bound queues, executor sizes, retries and timeouts. Roll transactions back before translating an HTTP error. No unmanaged background tasks, and no deployment migrations run from every worker's lifespan.
- No implicit lazy I/O during schema serialization, and no `Any` standing in for an unimplemented typing contract.
- Manual and generated APIs, docs, MCP and admin share the same public contracts. Generated and admin actions never bypass ownership or tenant policy.

## Tests and verification

- Get a green baseline before changing code, then run the full applicable suite with coverage at the end. Add meaningful tests for new behavior and uncovered changed paths; do not test inert prose or mirror the implementation.
- Tests use disposable databases, synthetic fixtures and fake recipients. Never create real accounts or send real messages. Never commit production dumps.
- Treat failures under load as evidence: rerun in isolation, keep the original result and tell host overload apart from a reproducible defect.
- For code changes that can affect performance, compare at least five current samples with a retained baseline and investigate regressions instead of moving thresholds. Do not present external performance claims as measurements of Hinoki.

## Documentation

- Every added or changed user-facing API, setting, CLI or MCP option, runtime behavior or admin feature updates its guide or reference in `docs/` in the same change, with a minimal example that matches the implementation. Keep navigation, `docs/guides.md` and page status aligned, and run a strict docs build.
- Public documentation describes Hinoki's own behavior directly. Comparative claims belong only in evidence-backed benchmark reports.

## Commits and pull requests

- One cohesive scope per commit and one matching conventional-commit type. Keep implementation, tests, documentation, dependencies/tooling and repository policy in separate commits.
- Branches are `<type>/<short-kebab-case-topic>` with the same types (`feat`, `fix`, `refactor`, `perf`, `test`, `docs`, `build`, `ci`, `chore`). Delete a branch once its pull request is merged: the remote branch goes automatically (the repository deletes head branches on merge; with `gh pr merge` keep `--delete-branch`), and you delete the local branch and its worktree.
- Pull request descriptions follow [`.github/pull_request_template.md`](.github/pull_request_template.md) and explain the change; they carry no personal messages.
- Every pull request carries one label for its type, set when it is opened (`gh pr create --label`): `feat` → `enhancement`, `fix` → `bug`, `docs` → `documentation`, `perf` → `performance`, `test` → `tests`, and `refactor`, `build`, `ci` and `chore` by their own names.
- Do not use `git checkout`, `git stash` or `git reset` to rearrange a tree shared with other work; use a separate worktree.
