---
status: implemented
related:
  - dev-mcp.md
  - rules.md
  - app-mcp.md
---

# Build with a coding agent

A project made by `hinoki new` is ready for a coding agent: rules already on, an `AGENTS.md` with the conventions, and a read-only MCP server that describes the app. This page shows the loop and where the agent's authority ends.

```sh
hinoki new shop    # asks for the directory, database, server, agent files, skills, git and uv sync
cd shop
pytest
hinoki check
```

The generated tests pass and `hinoki check` exits 0; this repository's own test suite generates a project and runs both on every change.

## What the project contains

| File | Purpose |
| --- | --- |
| `AGENTS.md` | Commands, conventions and where the agent's authority ends; `CLAUDE.md` points to it |
| `rules.py` | Eight rules on from the start (below); the model rules `HNK-DB-001` and `HNK-DB-002` commented out until the first model exists, because a model rule that matches no model fails |
| `.mcp.json` | Starts the [development MCP](dev-mcp.md) |
| `hinoki-rules.toml` | The owner's waivers |

| Rule | Promise |
| --- | --- |
| `HNK-ACC-001`, `HNK-ACC-002` | Every action names an access policy and a permission |
| `HNK-API-001`, `HNK-API-002` | Responses are concrete `Schema` types |
| `HNK-API-003` | Lists are paginated |
| `HNK-TXN-001` | Writes run in a transaction |
| `HNK-SCH-001` | Fields such as `password_hash` never appear in a response `Schema` |
| `HNK-SCH-002` | Request text has a length limit |

`AGENTS.md` states the same conventions as [structure an application](patterns.md): public only on purpose, `atomic=True` for writes, `Schema` classes in responses, explicit relation loading, server values win on create, migrations as reviewed SQL.

## Give the agent tools

* **The development MCP** lets the agent read the real registry, rules and models, explain one action or error code, check the project and draft a model. It never writes. See [development MCP](dev-mcp.md).
* **Skills** (optional): `hinoki skills install` writes four playbooks under `.claude/skills/`: `hinoki-add-endpoint`, `hinoki-fix-rule`, `hinoki-review-api` and `hinoki-test-api`. Each names the MCP tool or command to run and what belongs to the owner. See [`hinoki skills`](cli.md#skills).
* **Errors that say what to do**: validation errors list every problem with a path and a stable code, and a registration error names the action and parameter.

## The loop

1. The agent reads the project with `inspect_project`.
2. It makes one small change: a model, a `Schema`, an action.
3. It runs `pytest`, `ruff`, `mypy` and `hinoki check`.
4. A violation is a stable rule ID, a location and an explanation; the fix is a code change.

**Waivers are the owner's decision.** `hinoki-rules.toml` is read by the tools, never written, and a waiver needs a reason, an owner and a bounded target. An agent that edits it to pass a check breaks `AGENTS.md`; the framework cannot stop someone with write access, but the change is visible in review. Put the file under `CODEOWNERS`.

## Check a change against clients

```sh
hinoki openapi --output openapi-baseline.json
hinoki openapi --breaking-against openapi-baseline.json
```

Take the baseline from the released version. The second command exits `1` and lists each change that breaks an existing client: removed operations or response fields, new required input, narrowed enums, changed types. See [`hinoki openapi`](inspect.md#openapi).

## Generate request bodies

`request_cases` reads an OpenAPI document and returns one `RequestCase` per operation with a JSON body: `method`, `path`, a valid `body`, and `invalid`, a list of `(description, body)` pairs that each break one constraint.

```python
# tests/bodiestest.py
from hinoki.ai.samples import request_cases

from shop.main import app

CASES = request_cases(app.openapi())  # one per operation with a JSON body
```

Parametrize a test over `CASES`: send `case.body` and expect a `2xx`, then send each body in `case.invalid` and expect `422`.

It raises `CannotSample` for a schema it cannot build a value for, such as a regular-expression pattern. Fill path parameters with real values before sending, and use a disposable database.

## Limits

* The development MCP describes and checks a project. To let an agent use a running app, [expose selected actions over MCP](app-mcp.md).
* Rules check declarations, not what a function body does. Import rules read the project's own files and report dynamic imports as unverifiable.
* `propose_model` is a starting point: owner scoping, pagination and `unique_together`, but no business rules, state transitions or other relations.
* This workflow was verified by generating a project and running its tests and `check`, and by one fresh agent adding an owner-scoped resource from `AGENTS.md` alone. One run by one agent is evidence, not a benchmark.

## See also

* [Development MCP](dev-mcp.md)
* [Rules](rules.md)
* [Structure an application](patterns.md)
