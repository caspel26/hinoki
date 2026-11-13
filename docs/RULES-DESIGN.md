# Rules and development MCP — design (Q006)

Status: **R1 design, R2–R5 implemented** (2026-11-13): `python/hinoki/rules.py` with `Diagnostic`/`Rule`/`RuleSet`, `Actions.require_access` (`HNK-ACC-001`), `Transactions.require_atomic` (`HNK-TXN-001`), `Schemas.forbid_exposure` (`HNK-SCH-001`) and `HinokiAPI(rules=..., enforce_rules=...)`. `Imports.forbid` (`HNK-IMP-001`, static, AST only), waivers in `hinoki-rules.toml` (`python/hinoki/waivers.py`, notices `HNK-WVR-001/002`) and `python -m hinoki check` (`checkcli.py`; text/json, exit 0/1/2). R5: `python -m hinoki mcp` (`python/hinoki/mcp.py`, tools `inspect_project`, `list_rules`, `explain_action`, `check_project`). R6 (review) open. Source: `FRAMEWORK_SPEC.md` §14–15 (local package). Nothing here is implemented until a step below says so.

## Observed

* The registry already holds everything a startup rule needs: `HinokiAPI.routes` (method, path, handler, status, `Access`, `transaction`, pagination, parsers, renderers, owner class) and `bindings` (typed parameters, body schemas). `HinokiAPI.openapi()` is derived from it.
* Models carry `__table__`, columns, relations, unique groups; `Schema` classes expose `specs()`; `Access` names an auth provider and scopes.
* The only CLI is `python -m hinoki migrations …` (`__main__.py`). There is no rules code, no MCP code, no diagnostics type.
* Rules are for **applications built with Hinoki**, distinct from `AGENTS.md`, which governs developing Hinoki itself.

## Decisions

### D1 A rule is a typed, declarative object with a stable ID

```python
from hinoki.rules import RuleSet, Imports, Actions, Schemas, Transactions

project_rules = RuleSet(
    Imports.forbid(source="blog.domain", target="blog.api"),
    Actions.require_access(match="blog.api.*", except_=["health"]),
    Schemas.forbid_exposure(field="password_hash"),
    Transactions.require_atomic(methods={"POST", "PUT", "PATCH", "DELETE"}),
)
```

* Each rule class has an ID (`HNK-IMP-001`, `HNK-ACC-001`, …), a phase, a default severity and a one-paragraph explanation. IDs never change meaning; a retired rule keeps its ID.
* Custom checks subclass `Rule` (`check(self, project) -> Iterable[Diagnostic]`) and must declare `id`, `phase`, `effects`. A check that runs code, touches the network, a database or files declares that in `effects`; the CLI and MCP refuse effectful checks unless explicitly allowed.
* No LLM, no free-text instruction is ever evaluated as a rule.

### D2 Diagnostics are data first

`Diagnostic(rule_id, severity, phase, message, location, explanation, status)`; `location` is `file:line` when known (handlers: from `inspect`; imports: from the AST), else the action name; `status` is `violated` (demonstrated) or `unverifiable` (the check could not decide, e.g. a dynamic import). The two are never merged: the spec requires the message to distinguish them. JSON output has a `schema_version`; text output is derived from the same objects.

### D3 Phases and what each can honestly claim

| Phase | First rules | Limit stated in the diagnostic |
| --- | --- | --- |
| static | `Imports.forbid` (AST of the project's modules; transitive imports through third-party code are not followed) | dynamic imports (`importlib`, `__import__`) are reported `unverifiable`, not ignored |
| startup | `Actions.require_access`, `Schemas.forbid_exposure` (output schemas reachable from actions, recursively), `Transactions.require_atomic`, duplicate path/operation names | decides declarations, not what a function body does |
| runtime | none in this slice (design only: capability checks around ORM writes) | — |
| test/contract | none in this slice (`run_scenario` deferred) | — |

Rules apply when enabled in the `RuleSet`; creating an app without one runs nothing. `HinokiAPI(rules=project_rules, enforce=True)` makes startup-phase violations fail startup with every diagnostic listed; `enforce=False` only logs. Static rules run from the CLI/CI, never at import time or startup (no import-time I/O).

### D4 Waivers belong to the owner

* An exception is declared in `hinoki-rules.toml` next to the project: `[[waiver]] rule="HNK-ACC-001" target="blog.api.Posts.health" reason="..." owner="..." expires="2027-01-31"`. A waiver without `reason`, `owner` or a bounded scope is rejected; expired waivers are violations again. Waived diagnostics stay in the output with status `waived`.
* The tools (CLI, MCP) can **read** waivers and report them but never write them; there is no `--update-baseline`. The repository should require owner review for that file (documented CODEOWNERS example). This cannot be enforced against someone with write access to the repository; the design promises visibility and no tool-assisted bypass, nothing more.

### D5 CLI

`python -m hinoki check --app module:factory [--rules module:project_rules] [--format text|json] [--phase static|startup]`. Exit codes: 0 clean (or only waived/info), 1 violations, 2 usage/loading error. It builds the registry without starting lifespans and without any database or network access.

### D6 Development MCP

* `python -m hinoki mcp --app module:factory [--rules …]` speaks JSON-RPC 2.0 over stdio (MCP): `initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call`. Stdlib only. It is a separate command; `hinoki.runtime` never imports it, so a production server cannot publish it by accident.
* Tools, all read-only and versioned (`tool_version` in every result): `inspect_project` (actions, models, `Schema` types, parsers/renderers, rules enabled), `list_rules`, `explain_action` (signature, `Schema` types, access, transaction, documented errors, applicable rules, source location), `check_project` (the same diagnostics as the CLI; effectful checks refused). `run_scenario` is **deferred**: it needs a scenario format and an authorisation story.
* Results are structured JSON in `structuredContent` plus a short text rendering. Errors use JSON-RPC errors for protocol faults and `isError` results for tool failures.
* Unverified: conformance with a specific MCP client or protocol revision. The implementation follows the published method names and shapes from memory of the specification; a real client run is a listed open item, and no compatibility claim is made until it exists.
* Application MCP (publishing selected actions) is a later item and not designed here.

## Alternatives considered

* Rules as comments/docstrings or YAML only: not typed or composable. Rejected.
* Running rules on every startup by default: violates "optional" and slows boot. Rejected; explicit `rules=`.
* Using an MCP SDK dependency: not chosen to keep installation light; the surface used is small. Revisit if protocol churn makes hand-maintenance costly.

## Failure and empty states

Unknown app/rules import: exit 2 with the import error. Empty rule set: "0 rules" and exit 0. A rule that raises: reported as an `internal` diagnostic for that rule (other rules still run); it is a failure, never a pass. Unresolvable handler locations fall back to the action name. MCP: unknown tool → JSON-RPC error `-32602`; malformed JSON → `-32700`; a tool exception → `isError` result without a traceback.

## Tests

Per rule: a passing and a failing fixture with the exact diagnostic (ID, location, status); `unverifiable` for a dynamic import; transitive schema exposure; waiver lifecycle (missing reason, expired, scoped, never written by tools); enforce vs log-only at startup; CLI exit codes and JSON schema; MCP over a pipe (handshake, list, each tool, unknown tool, malformed input, effectful check refusal); import-graph test that `hinoki.runtime` does not import `hinoki.mcp`.

## Steps

| Step | Deliverable | Files |
| --- | --- | --- |
| R1 | This design | `docs/RULES-DESIGN.md` |
| R2 (done) | `Diagnostic`, `Rule`, `RuleSet`, startup rules, enforcement hook on `HinokiAPI` | `python/hinoki/rules.py`, `python/hinoki/app.py` |
| R3 (done) | Static `Imports.forbid`, project loading | `python/hinoki/rules.py`, `python/hinoki/imports.py` |
| R4 (done) | Waivers (`hinoki-rules.toml`) and `python -m hinoki check` | `python/hinoki/waivers.py`, `python/hinoki/__main__.py` |
| R5 (done) | Development MCP server and tools | `python/hinoki/mcp.py` |
| R6 (done) | Independent review (rules decide what passes CI; MCP exposes the project), fixes, docs, handoff | — |

## Open (owner)

Whether `enforce` should default to on when a `RuleSet` is passed (recommended: yes); a scenario format for `run_scenario`; which MCP revision to target once a client is available to test against.

## Implementation notes (R5)

* `mcp.py` is stdlib-only JSON-RPC 2.0, one JSON message per line on stdin/stdout; protocol faults are JSON-RPC errors (-32700/-32600/-32601/-32602), tool failures are `isError` results without tracebacks, notifications get no answer. `initialize` echoes a known protocol version (`2025-06-18`, `2025-03-26`, `2024-11-05`) or offers the newest.
* A test proves `hinoki.mcp` is not in `sys.modules` after building and serving a `HinokiAPI`; the command loads it lazily.
* Effectful rules are never run through the server (no `allow_effects`). Waivers are read only. `--models` optionally lists modules whose models `inspect_project` describes.
* **Not verified against a real MCP client**; the shapes follow the specification from the author's knowledge. `run_scenario` and the application MCP stay out of scope.

## Review outcome (R6)

An independent read-only review (Opus-class) found 2 high, 8 medium and 8 low defects. Fixed, each with a regression in `tests/reviewrulestest.py`:

* **Identity:** an action is named after the API class it is mounted on (`pkg.Posts.remove`), not the base class that defines the handler, so inherited actions are matched, waived and explained separately. `except_` patterns match the full qualified name only (`*.health`, never a bare `health`).
* **Fail closed:** a rule that could not run (needs effects, crashed, `SystemExit`, matched nothing, scanned nothing, return type too open to check) is an `unverifiable` **error**; its diagnostic has an empty subject, which no waiver can cover. `check --allow-effects` runs effectful rules; the MCP never does.
* **Waivers:** wildcard targets need a literal prefix of at least 5 characters containing a `.`; fields must be text; an explicit `--waivers` path that does not exist is an error; `list_rules` reports the waiver file path and whether it exists.
* **Rules:** exposure follows subclasses (the runtime serialises the actual subclass); dynamic imports resolve aliases, relative names with `package`/`__package__`, `fromlist` and keyword arguments.
* **MCP:** application output cannot reach the protocol stream (stdout is redirected to stderr while serving); results are normalised to plain JSON; no input can end the loop; ids and params are checked strictly; notifications never get a reply; `explain_action` finds inherited actions by `Class.method`.

Documented limits: JSON-RPC batches get one `-32600`; a failed `enforce()` reruns the rules on every later call (fails closed); unverifiable/refused diagnostics are not logged when `enforce_rules=False`; `--package a.b` imports `a` to locate it.
