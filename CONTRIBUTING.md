# Contributing to Hinoki

Hinoki is pre-alpha. Read [`CLAUDE.md`](CLAUDE.md) (the working rules, also for AI agents; `AGENTS.md` links to it), [`ROADMAP.md`](ROADMAP.md) (public project status) and [`docs/toolchain.md`](docs/toolchain.md).

```sh
uv sync --locked
uv run --locked pre-commit install
uv run --locked ruff check --fix . && uv run --locked ruff format .
uv run --locked mypy && uv run --locked pytest --cov
cargo fmt --all --check && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace
```

* Discuss non-trivial changes in an issue before implementing them; record final architecture decisions as an ADR in `docs/adr/`.
* Name branches `<type>/<short-kebab-case-topic>` using `feat`, `fix`, `refactor`, `perf`, `test`, `docs`, `build`, `ci` or `chore` (for example, `feat/route-groups`). Merged branches are deleted from the remote automatically; delete your local copy.
* Split commits by cohesive scope (`feat(core):`, `test(core):`, `docs(runtime):`, `build:`, `ci:`); do not combine implementation, tests, docs, dependencies/tooling or repository policy in one catch-all commit.
* For user-facing code changes, include a minimal usage example in the pull request description and update the relevant guide/reference and checked-in example in the same work item. If no user-facing code is changed, mark the template section as not applicable and explain why.
* The docsite publishes merges to `main` as `/dev/`; only `vX.Y.Z` release tags update versioned release docs and the `stable`/`latest` aliases. See [documentation versions](docs/documentation-versions.md).
* Add tests for every new feature or behavior change to the automated test suite (`tests/` for Python, `cargo test` for Rust) in the same pull request; bug fixes include a test that fails without the fix.
* Fuzz targets for the Kiso request-handling functions live in `fuzz/` and run in CI (`.github/workflows/fuzz.yml`). Locally they need a nightly toolchain and `cargo install cargo-fuzz --locked`: `cargo +nightly fuzz run redirect_location -- -max_total_time=60`. A new function that parses or validates client bytes gets a target.
* Don't claim a test, benchmark or feature that does not exist. Don't add Pydantic, Django, FastAPI or Starlette.
* The README demo (`assets/readme/demo.gif` and its still `demo.png`) is rendered from real `hinoki` output; re-record it after a prompt or message in `hinoki new`, `hinoki check` or `hinoki dev` changes. With `ffmpeg`, `fc-match` (fontconfig), `rsvg-convert` (librsvg), `git` and `curl` on the PATH (on macOS, Homebrew provides them), port 8000 free and `uv sync --locked` done, run `uv run --with pyte==0.8.2 --with pillow==12.0.0 --with fonttools==4.60.1 --with brotli==1.1.0 python scripts/readme_demo.py` from the repository root. The renderer's packages are pinned on that command line only; they are not project dependencies. The script fails instead of writing a GIF when the session shows an unexpected screen.
