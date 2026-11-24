---
status: implemented
related:
  - dev-mcp.md
  - rules.md
  - app-mcp.md
---

# Build with a coding agent

A project made by `hinoki new` is ready for a coding agent: rules already on, an `AGENTS.md` with the conventions, and an MCP server that describes the app. This page shows the loop and where the agent's authority ends.

```sh
hinoki new shop    # asks for the directory, database, server, agent files, skills, git and uv sync
cd shop
uv sync            # unless you answered yes to uv sync
uv run pytest
uv run hinoki check
```

The generated tests pass and `hinoki check` exits 0; this repository's own test suite generates a project and runs both on every change.

## Hinoki at a glance

A model that has seen other Python web frameworks tends to write their code. This summary is the part of these docs that corrects that; it is also the first block of [`llms.txt`](#docs-for-agents).

<!-- llms:start -->
* Import from `hinoki`: `Hinoki`, `Schema`, `Access`, `RouteGroup`, `http`, `db` and `crud`. A model class comes from `hinoki.orm` (`from hinoki.orm import Model, field, pk`).
* Declare actions with `@http.get`, `@http.post`, `@http.put`, `@http.patch`, `@http.delete` or `@http.query`, then pass them to `Hinoki(include=[...])`. Parameters and the return type are annotations.
* Name an `Access` policy with `access=`; leave it out only for an action that is public on purpose. Writes use `atomic=True`, alone or on a `RouteGroup`.
* Request bodies and responses are `Schema` classes. Never return a `Model`: convert it with `XOut.from_model(row)`.
* Load relations explicitly with `select_related(...)` or `prefetch_related(...)`; nothing loads lazily.
* `crud(Model, ...)` generates list, retrieve and write actions for a model.
* Run `hinoki check` after each change; `hinoki mcp` serves the real registry, rules and models to the agent.
* Does not exist here: `BaseModel`, `Depends`, `APIRouter`, `@app.get(...)` and `with get/post` blocks.

```python
from hinoki import Hinoki, Schema, http


class Greeting(Schema):
    message: str


class GreetingIn(Schema):
    name: str


@http.get("/greeting")
async def read_greeting() -> Greeting:
    return Greeting(message="hello")


@http.post("/greeting", atomic=True)
async def create_greeting(body: GreetingIn) -> Greeting:
    return Greeting(message=f"hello {body.name}")


app = Hinoki(include=[read_greeting, create_greeting])
assert "/greeting" in app.openapi()["paths"]
```
<!-- llms:end -->

A test runs this block against the installed package, so the names and syntax above stay true. See [function-based APIs](function-endpoints.md) for `RouteGroup` and [structure an application](patterns.md) for the conventions.

## What the project contains

| File | Purpose |
| --- | --- |
| `AGENTS.md` | Commands, conventions, a pointer to [Hinoki's docs for agents](#docs-for-agents) and where the agent's authority ends; `CLAUDE.md` points to it |
| `rules.py` | Eight rules on from the start (below), listed in `hinoki.toml` as `[project] rules = ["<package>.rules:project_rules"]` so the application loads them without a `rules=` argument; the model rules `HNK-DB-001` and `HNK-DB-002` commented out until the first model exists, because a model rule that matches no model fails |
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

* **The development MCP** lets the agent read the real registry, rules and models, explain one action or error code, check the project and draft a model. It never writes source code, rules or waivers; only `run_scenario`, when you enable it, runs real handlers. See [development MCP](dev-mcp.md).
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

`hinoki llms` renders the same document as an [llms.txt](openapi.md#llms) text of your application's own operations, for an agent that works on a client of it; `Hinoki(llms_url="/llms.txt")` serves it. Neither is on by default.

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

## Docs for agents

Every build of this site also publishes the documentation as plain Markdown, in the layout of [llmstxt.org](https://llmstxt.org/):

| File | Content |
| --- | --- |
| `llms.txt` | An index: the block above, then one link and description per page, grouped like the guides. Reference, project pages, the changelog and benchmarks are under `Optional` |
| `llms-full.txt` | Every page except the `Optional` ones, concatenated in navigation order |
| `<page>.md` | One page as Markdown, for example `crud.md` or `tutorial/1-models.md`; links inside it are absolute, and a `reference/` page lists each documented name with its signature and a link to the rendered page |

The generated `AGENTS.md` points an agent at `llms.txt` and tells it to prefer these files and the development MCP over guessing from other frameworks.

Each documentation version has its own copy, for example `https://hinoki.caspel.dev/dev/llms.txt` and `https://hinoki.caspel.dev/dev/crud.md`; the root `https://hinoki.caspel.dev/llms.txt` follows the default version (see [documentation versions](documentation-versions.md)). The files come from the same pages as the site, and the build fails when a link or a description is missing.

## Limits

* The development MCP describes and checks a project. To let an agent use a running app, [expose selected actions over MCP](app-mcp.md).
* Rules check declarations, not what a function body does. Import rules read the project's own files and report dynamic imports as unverifiable.
* `propose_model` is a starting point: owner scoping, pagination and `unique_together`, but no business rules, state transitions or other relations.
* This workflow was verified by generating a project and running its tests and `check`, and by one fresh agent adding an owner-scoped resource from `AGENTS.md` alone. One run by one agent is evidence, not a benchmark.

## See also

* [Development MCP](dev-mcp.md)
* [Rules](rules.md)
* [Structure an application](patterns.md)
