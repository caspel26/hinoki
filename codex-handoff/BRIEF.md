# Brief for Codex: remaining Hinoki work

This directory is a transport for local working notes. It must NEVER be merged into `main`.
When the work is picked up, copy the six `*-DESIGN.md` files into `docs/` in your checkout (that
path is gitignored by `docs/*-DESIGN.md`, so they stay local; the text refers to them as
`docs/NAME-DESIGN.md`), then delete the branch `docs/codex-handoff` and close its PR.

Read first, in this order: `AGENTS.md`, `ROADMAP.md`, `docs/TOOLCHAIN.md`, then
`RUST-PATH-DESIGN.md` (the plan), `ROWS-RUST-DESIGN.md`, `RUST-SERVER-DESIGN.md` (server design,
sections 3 and 8), `NATIVE-AWAIT-DESIGN.md`, `R8-H2-DESIGN.md`, `ADAPTER-PREPARED-PLAN-DESIGN.md`.

## Project direction (owner)

Hinoki is a Python framework whose engine is Rust: put as much as possible in Rust. Hinoki must
not become a copy of Django Bolt; Bolt is only a benchmark reference. The owner writes Italian or
English: always reply in Italian; everything committed (code, comments, docs, commit messages) is
English.

## State (2026-11-16)

* `main` includes the Rust HTTP server (hyper + tokio, rustls TLS, HTTP/2 over TLS with `server.http2`,
  default off), selectable with `--server rust` on `hinoki serve|dev`. The default server is Granian
  and stays so until the owner approves a flip.
* Known HTTP/2 limits, documented, to fix later: a response stalled for `write_timeout` closes the
  whole connection with a graceful GOAWAY (resetting only that stream needs the `h2` crate driven
  directly); the reset-flood bound is per connection; a stream opened in the race with an idle
  GOAWAY is cut at `shutdown_timeout`; the backend is not told that a reset stream's request was
  cancelled; the last byte of each HTTP/2 response is its own 1-byte DATA frame so the stall watcher
  covers it (reviewer measured about +20% time on many small concurrent responses, only with
  `http2` on; a size threshold is a possible optimisation).
* Measurements so far (relative only, hosts not quiet, floors for `GET /health`, no DB):
  Rust-only route 8.6 us/request; Rust calling one trivial sync Python handler 11.0 us; Hinoki on
  today's Rust server 23.1 us; Hinoki on Granian 30.4 us; minimal ASGI on Granian 9.6 us; bare
  hyper 7.6 us; actix-web 7.9 us (no advantage over hyper). Profile of the Python layer on
  `/health`: Hinoki Python adapter about half, Python builtins about 20%, asyncio about 11%,
  Rust calls only 6-7%. Preliminary A/B of the two servers on the comparative app: no measurable
  difference (throughput +0.6..3.4%, p99 worse on the Rust server in all four scenarios).
  No performance claim exists or may be made without retained raw measurements.

## Work, in order

1. **Lane B design decision (owner first).** `RUST-PATH-DESIGN.md` section 12 (S3): the asyncio fallback
   cannot happen after the Rust driver has advanced a coroutine, so a handler must be declared
   native-only BEFORE the first `send` (explicit marker, restricted API of native awaitables, or a
   hybrid driver). Present options with code examples to the owner; no code before the choice. Then
   build lane B (one GIL entry, coroutine driven from Rust with a loop-independent awaitable that
   yields a marker; the ORM bridge today is bound to a running asyncio loop), differential tests
   against lane C (today's pipeline), independent review of concurrency, cancellation and
   contextvars/caller identity.
2. **Rows in Rust** (`ROWS-RUST-DESIGN.md`, option A: `RowPlan` + `Database.fetch_models`), A/B on
   `list_20`/`list_100` under real Granian, alone, alternating runs.
3. **Lane A (Rust only):** static routes, rows-to-JSON, native auth (JWT and permissions in Rust),
   generated CRUD list/detail then writes. Each declarative form that replaces a Python callback is
   public API: design it with the owner one at a time. `hinoki routes` must show each route's lane.
4. **Default-flip gate** (`RUST-SERVER-DESIGN.md` 8.2): only with owner approval after the final
   benchmark.
5. **Final benchmark** after everything above, on a quiet host: Granian vs Rust server (with and
   without TLS, HTTP/2 small responses, each lane), five alternating runs, CV recorded, `list` on a
   freshly seeded table, attention to p99, raw JSON retained under `benchmarks/comparative/results/`.
   Method and scenarios: `benchmarks/comparative/README.md` and `run.py` (add a local Rust variant;
   `hinoki serve TARGET --server rust --workers 4`).
6. **Last of all:** rewrite the README and the GitHub repository description/topics to be more
   generic (not tied to one component) and more detailed (what Hinoki is, features, honest status
   and limits). Show the proposed text to the owner before `gh repo edit`.

## Working rules

* Plan before non-trivial code: read the nearest code and docs, list every place the change reaches,
  state plan and assumptions. Smallest complete change in the repo idiom, cognitive complexity 15
  or less per function, no functions nested in functions, no underscores in new file names (tests
  use `*test.py`), docs are part of the change.
* Verify by running: `cargo fmt --check`, `cargo clippy --workspace --all-targets -- -D warnings`,
  `cargo test --workspace --exclude hinoki-bindings` (the bindings crate does not link under
  `cargo test` on macOS), `uv sync --locked`, `pytest`, `ruff check` and `ruff format --check`,
  `mypy`, `complexipy . --failed`, `mkdocs build --strict`. Large UI work in a real browser.
  Report only what you ran and read.
* Security-sensitive work (auth, protocol, concurrency, cancellation) gets an independent read-only
  review with concrete defects and failing scenarios; fix findings with regression tests that fail
  without the fix. Several review rounds were needed on HTTP/2; expect it.
* Git: one logical change per commit, Conventional Commits (`type(scope): imperative lowercase
  subject`), never `--no-verify`, never force-push a shared branch, work in a git worktree from
  `origin/main`. Per closed task: push, open a PR, merge when all required checks are green on the
  exact head (`gh pr update-branch` when BEHIND, squash, then delete branch and worktree). PR
  descriptions are explanatory only (what changes and how it behaves). Pushing a release or force-push
  needs explicit owner approval. Never commit secrets or files over 1 MB.
