<p align="center">
  <a href="https://caspel26.github.io/hinoki/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="assets/readme/lockup-dark.svg">
      <img src="assets/readme/lockup-light.svg" alt="Hinoki" width="320">
    </picture>
  </a>
</p>

<p align="center"><strong>Python APIs with architecture you can check.</strong></p>

<p align="center">
  <a href="https://github.com/caspel26/hinoki/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/caspel26/hinoki/ci.yml?branch=main&label=CI&labelColor=1d1a24"></a>
  <img alt="Python 3.12, 3.13 and 3.14" src="https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-7a55c4?labelColor=1d1a24">
  <a href="https://caspel26.github.io/hinoki/dev/status/"><img alt="Status: pre-alpha" src="https://img.shields.io/badge/status-pre--alpha-e3c29b?labelColor=1d1a24"></a>
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-7a55c4?labelColor=1d1a24"></a>
</p>

Hinoki is a Python framework for HTTP APIs with a Rust core. An endpoint declares its types, who may call it, whether it runs in a transaction and which errors it returns; that one declaration drives validation, responses and the OpenAPI document. Optional rules check those declarations, and your imports, in CI.

> [!WARNING]
> Pre-alpha, not released. The API and migration format may change. The [`hinoki` package on PyPI](https://pypi.org/project/hinoki/) is unrelated: evaluate this one from a source checkout, with disposable data.

## Example

The [Notes example](examples/notes), condensed to one file:

```python
from typing import Annotated
from uuid import UUID, uuid4

from hinoki import Field, Hinoki, Schema, http
from hinoki.db import DatabaseModule
from hinoki.orm import Model, field


class Note(Model):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str = field(max_length=80)
    body: str = ""


class NoteIn(Schema):
    title: Annotated[str, Field(min_length=1, max_length=80)]
    body: str = ""


class NoteOut(Schema):
    id: UUID
    title: str
    body: str


@http.post("/notes", status=201, transaction=True)
async def create(data: NoteIn) -> NoteOut:
    return NoteOut.from_model(await Note.acreate(data))


app = Hinoki(
    include=[create],
    modules=[DatabaseModule("sqlite://./.hinoki/local.sqlite3")],
    openapi_url="/openapi.json",
    docs_url="/docs",
)
```

Only what a `Schema` names leaves the app, and invalid input is answered `422` before the handler runs:

```json
{"error": "validation_error", "request_id": "…", "details": [{"path": "data.title", "code": "min_length", "message": "at least 1 long"}]}
```

## Try it

You need [uv](https://docs.astral.sh/uv/) and a Rust toolchain; the core builds from source.

```sh
git clone https://github.com/caspel26/hinoki && cd hinoki
uv sync --locked
cd examples   # a project with three apps; the environment is one level up
uv run --project .. hinoki migrations sql --app notes 0001-create-table-notes   # review it
uv run --project .. hinoki migrations apply --app notes                         # local SQLite
uv run --project .. hinoki dev notes.main:app
```

Open `http://127.0.0.1:8000/docs`. Tables only come from migration files you review and apply. `hinoki new NAME` scaffolds your own project; the [quick start](https://caspel26.github.io/hinoki/dev/quick-start/) walks through each step.

## Access and transactions, declared once

Condensed from the [Tracker example](examples/tracker):

```python
member_only = Access(auth=MemberTokens(), permissions=["tasks:use"])
tasks = RouteGroup("/tasks", access=member_only, atomic=True)  # writes run in a transaction


@tasks.post("/{task_id}/complete", errors=(errors.NotFound, TaskAlreadyDone))
async def complete_task(task_id: UUID, caller: Caller[Member]) -> TaskOut:
    return out(await operations.complete_task(caller.user.id, task_id))
```

The policy and the declared `404` and `409` appear in the OpenAPI document, and `hinoki routes` lists who may call each route.

## Rules you can check

```python
from hinoki import Actions, Imports, Schemas, Transactions
from hinoki.rules import RuleSet

tracker_rules = RuleSet(
    Actions.require_access(match="tracker.*", except_=["tracker.api.health"]),
    Transactions.require_atomic(except_=["hinoki.mcpapp.*"]),
    Schemas.forbid_exposure("token"),
    Imports.forbid(source="tracker.models", target="tracker.api"),
)
```

`hinoki check` reads the app and its source without a database, reports each violation with its file, line, rule ID and fix, and exits non-zero:

<p align="center">
  <img src="assets/readme/check.svg" width="760" alt="hinoki check reports three rule violations, each with its file and line, a rule ID and a fix, then a summary: 2 rules, 3 violated">
</p>

## Python handlers, Rust core

Handlers are ordinary Python, sync or async, served over ASGI by Granian or by an opt-in Rust HTTP server. The compiled extension does path matching, selected query, header and cookie parsing, schema validation and serialization, cryptography, and the ORM's SQL compilation and execution. The ASGI adapter, handler calls and most of the request pipeline run in Python.

Also included: an ORM for SQLite, PostgreSQL, MySQL and MariaDB with relations and reviewed migrations, generated CRUD, typed settings, OpenAPI 3.1 with Swagger UI and ReDoc, test clients, MCP servers for apps and coding agents, and an optional admin.

## Status

Tested in CI on Linux with Python 3.12 to 3.14. Not available yet: SQL Server and Oracle, streaming responses, WebSockets, cursor pagination, per-route CORS. See the [status page](https://caspel26.github.io/hinoki/dev/status/) for every current boundary.

[Documentation](https://caspel26.github.io/hinoki/) · [Tutorial](https://caspel26.github.io/hinoki/dev/tutorial/) · [Guides](https://caspel26.github.io/hinoki/dev/guides/) · [Reference](https://caspel26.github.io/hinoki/dev/reference/) · [Examples](examples) · [Contributing](CONTRIBUTING.md) · MIT [license](LICENSE)
