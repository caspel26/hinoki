# Agent instructions — Hinoki

Status: adopted 2026-11-13 from `framework-handoff/rules/AGENTS.md` (name Aptum → Hinoki; otherwise unchanged).

**Document locations.** The design package lives unmodified in `framework-handoff/` (spec, syntax contract, runtime, provenance, `sources/` historical snapshots — never edit `sources/`). Those documents still say "Aptum"; read it as **Hinoki** (`hinoki` import/CLI, `HinokiAPI` application class, `API` for endpoint groups). The live work queue is the root `handoff.md`; designs go in `docs/`, ADRs in `docs/adr/`.

**Toolchain.** `uv` for Python (`uv run --locked`), Ruff for lint+format, mypy for types, Cargo for Rust, maturin as build backend. See `docs/TOOLCHAIN.md`.

## Read first

Read this file, `handoff.md`, and `framework-handoff/{FRAMEWORK_SPEC,SYNTAX_CONTRACT,RUNTIME_AND_MODULES}.md` before starting. Durable decisions and the ordered work queue live in files. Read the design for the active item rather than reconstructing intent from chat.

The project name is **Hinoki** (owner decision, supersedes the earlier "Aptum" in the handoff package). `hinoki` is the package/import/CLI identifier; external availability on PyPI/crates.io has not been checked. Do not invent implemented features, benchmark numbers, supported versions or completed checks.

## Product boundaries

- Independent Python framework; do not build it on Django, FastAPI, Starlette or another web framework. Infrastructure servers such as Granian are allowed.
- Substantial Rust core for request processing/routing and ORM execution. Document the real server/binding boundary; do not call an ASGI path entirely native if it crosses Python objects.
- Own public schema/settings interfaces; no Pydantic dependency introduced as a convenience.
- Function and class APIs, multiple custom methods per API, optional CRUD generation, optional architectural rules, public authentication default.
- Confirmed base syntax: `@get`, `@post`, `@put`, `@patch`, `@delete`; reusable typed `Access` policies; `access` and `transaction` options on the HTTP decorator. Preserve normal Python and decorator composition.
- Do not reintroduce declaration blocks such as `with get/post`, or inline `get(query=...)`/`lookup=...` endpoint assignments. CRUD generation remains required, but its advanced syntax is still provisional.
- Differentiate Hinoki through deterministic optional rules, structured AI/MCP context, coherent customization and explicit modules, not extra syntax machinery.
- Typed built-in settings, automatic/customizable docs and pagination.
- Vocabulary: write `Schema` (code font) for the Hinoki class; use "table state"/"migration state", "OpenAPI document" and "JSON Schema" for the other meanings (`docs/GLOSSARY.md`). The old `Contract` alias no longer exists.
- Optional admin extra with explicit activation and migrations; installation/import never mutates a database.
- Explicit application modules and dependencies; defined worker lifespan, bounded sync execution, startup and graceful shutdown. Granian is the first server candidate, pending integration validation.
- Modules within a service may share local contracts. Remote services do not share ORM joins or pretend to share local transactions.

## Work loop

1. Add new work to the ordered queue in `handoff.md`; finish the active item before switching unless the owner redirects it. Answer questions promptly and incorporate corrections into the active design.
2. Design nontrivial changes before implementation. Write `docs/<TOPIC>-DESIGN.md`: observed code/data, one recommendation, alternatives and trade-offs, public API examples, failure/empty/private states, tests, numbered build steps, file ownership and dependencies. Recommend defaults for routine choices; isolate genuine owner decisions.
3. Implement one reviewable step at a time. When multiple agents are authorized, parallelize only independent tracks. Shared files require explicit non-overlapping ownership or serial edits. Never have two agents edit the same function concurrently.
4. Verify the actual result. A delegated agent's report is not sufficient evidence. Inspect the diff and run the applicable checks independently. Report exactly what ran and what did not.
5. For UI changes, inspect actual screenshots and exercise changed controls with browser automation. Check desktop 1440×900, phone 390×844, narrow 320 and short 360×740 where relevant. Verify keyboard/touch paths and both desktop/mobile behavior; test another relevant browser for gestures. Screenshots alone do not prove interaction correctness.
6. Obtain an independent, read-only review for data, authentication, authorization, privacy, migrations, concurrency or similarly high-impact changes. Require concrete defects, file/line, severity and a failing scenario. Fix findings and re-run affected checks.
7. When commits are in scope, make scoped local commits, include the applicable `Co-Authored-By` trailer and update `handoff.md` in the same series. Do not push, publish or deploy without the owner's authorization. Previously given authorization remains valid within its scope.

No feature freeze or Shiori deployment flow is imported into this project. A future freeze or branch policy must be recorded explicitly.

## Agent/model split inherited from Shiori

| Work | Model in original workflow |
| --- | --- |
| Design, planning, independent review, audits, visual critique, product decisions and sensitive decisions | Opus |
| Multi-file implementation, features, refactors and reasoning-heavy fixes from the design | Sonnet |
| Exact mechanical changes, formatting, reconciliation and small well-specified tasks | Haiku |

Use the cheapest suitable model; escalate after two failed attempts. A delegation brief must include repository path, exact documents, allowed files, no-touch areas, checks and expected report format.

These names preserve the owner's source workflow. If a named model or delegation capability is unavailable, state the limitation and record a proposed role-equivalent mapping; do not pretend that the required independent review happened. The availability of this draft does not itself authorize parallel agents in a session that disallows them.

## Tests, coverage and performance

Carry forward the django-ninja-aio-crud verification discipline:

1. Establish a green baseline before changing code. Run fast checks while implementing.
2. At completion run the full applicable suite with coverage; add meaningful tests for new behavior and uncovered changed paths. Maintain or improve coverage. Do not add tests that merely mirror an implementation or test inert prose.
3. Run the project's performance suite for code changes, with at least five comparable current samples and a retained comparable baseline, then analyze regressions. The inherited starting detector compares five baseline and five current runs, with a 15% threshold and a 2σ heuristic. Treat these as a detector policy, not proof of a universal confidence level.
4. Investigate unexpected variance; CV above 15% is a signal to inspect load and reproducibility, not proof that a regression is harmless. Collect more samples when justified. Never discard a failing result merely because it is inconvenient.
5. Fix unjustified regressions. Document necessary costs and the affected workloads; do not quietly move the baseline or thresholds to hide them.
6. Keep machine-readable benchmark data and a human-readable report. Compare equivalent payloads, database state and concurrency; disclose driver/runtime differences. Never copy performance claims from another project as evidence for this one.

The source repository's Django commands do not apply here. The first bootstrap task must create and document actual Python/Rust fast/full/coverage/benchmark entry points. Until they exist, report them as unavailable rather than claiming that placeholder commands passed.

Expected gate categories: Python lint/format, Python type checks and typing fixtures, Python tests/coverage, Rust formatting/lints/tests, native-binding integration, real disposable-DB tests, generated OpenAPI checks, packaging/wheel checks, performance. Admin/browser gates apply when that module changes. Exact commands and scope belong in `handoff.md` and CI.

Once a failing check has been fixed and the required checks pass, repeat or broaden runs only for new changes or unresolved evidence. Documentation-only changes need document/link/example validation rather than an unrelated DB benchmark cycle.

## Code and architecture conventions

- Use type annotations on public function signatures and class attributes. Preserve signatures in decorators with `ParamSpec`/`TypeVar` and `functools.wraps` where appropriate.
- Imports at the top of Python files. A local import is allowed to avoid a circular dependency, with an explanatory comment.
- Adopt Ruff linting/formatting and pre-commit checks for AST, conflict markers, TOML/YAML, trailing whitespace and EOF. Choose tool versions for this project; do not copy old pinned versions as a compatibility claim.
- Keep complexity controlled. Shiori's cognitive-complexity budget of 15 is a design target; choose the equivalent supported tooling per language before enforcing a numeric gate.
- Explicit resource ownership: no import-time I/O, connection pools created per worker after process start, no async pool across event loops, no global mutable request context.
- Bound queues, executor sizes, retries and timeouts. Transactions roll back before HTTP error translation. Do not launch unmanaged background tasks or run deployment migrations from every worker's lifespan.
- Do not introduce implicit lazy I/O during schema serialization, or `Any` as a substitute for an unimplemented typing contract.
- Keep public contracts shared by manual/generated API, docs, MCP and admin. Do not silently bypass ownership or tenant policy in generated/admin actions.

## Data and shared workspace safety

Use disposable databases, synthetic fixtures and fake mail recipients. Never create real accounts or send real messages as a test. Production dumps contain sensitive data: do not commit, paste or upload them; use authorized encrypted handling and sanitized disposable copies only.

Do not use `git checkout`, `git stash` or `git reset` to manipulate a shared tree. Inspect status before editing and use isolated worktrees for comparisons or parallel work. Preserve unrelated changes. Do not reuse another test run's ports, databases or fixture output directory.

Treat test failures under load as evidence to investigate: rerun in isolation when appropriate, retain the original result and distinguish host overload from a reproducible defect. Use deterministic clocks/fixtures for date-dependent behavior.

## UI and docs

Use shared components and tokens; new variants need a reason. Inspect hierarchy, spacing, alignment, repetition and the framework's own visual identity. Do not import Shiori-specific branding, provider restrictions or catalog rules.

Update docs for user-facing API changes and provide minimal examples, including sync/async differences and errors. Verify the examples against the actual implementation once it exists. Keep generated docs and runtime contracts consistent.

## Releases

Derive release notes from the actual diff. Use the inherited sections New Features, Improvements, Documentation and Summary, contextual emojis, file-path blockquotes and examples for new APIs. Keep styling/config documentation brief. Do not add a detailed Tests section to release notes; verification still belongs in the PR/handoff report.

The source CLAUDE.md explicitly overwrites CHANGELOG.md for each release. That original rule is preserved in `sources/`. For this new project, whether CHANGELOG.md is a latest-release snapshot or cumulative history is an open adoption decision. Do not destroy existing release history while the decision is pending.

Once the project defines its release layout, update all actual version sources, docs selectors if present and completed queue entries together. Do not copy `ninja_aio/__init__.py` or Django docs workflow paths into this project.

## Completion report

State what changed, why, validation actually performed, known limitations and the next queue item. Keep `handoff.md` current with state, decisions, migrations, commands and unresolved failures. Never describe a design proposal as already implemented.
