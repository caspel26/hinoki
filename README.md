<p align="center">
  <a href="https://caspel26.github.io/hinoki/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="assets/readme/lockup-dark.svg">
      <source media="(prefers-color-scheme: light)" srcset="assets/readme/lockup-light.svg">
      <img src="assets/readme/lockup-light.svg" alt="Hinoki" width="380">
    </picture>
  </a>
</p>

<h3 align="center">Python APIs with architecture you can check.</h3>

<p align="center">
  Typed endpoints, declared access and transactions, a PostgreSQL and SQLite ORM,<br>
  and rules that fail the check when the code breaks them. Rust core.
</p>

<p align="center">
  <a href="https://caspel26.github.io/hinoki/"><strong>Documentation</strong></a> &nbsp;·&nbsp;
  <a href="https://caspel26.github.io/hinoki/dev/quick-start/">Quick start</a> &nbsp;·&nbsp;
  <a href="https://caspel26.github.io/hinoki/dev/tutorial/">Tutorial</a> &nbsp;·&nbsp;
  <a href="examples">Examples</a> &nbsp;·&nbsp;
  <a href="https://caspel26.github.io/hinoki/dev/status/">Status</a>
</p>

<p align="center">
  <a href="https://github.com/caspel26/hinoki/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/caspel26/hinoki/ci.yml?branch=main&label=CI&labelColor=1d1a24"></a>
  <a href="https://codecov.io/gh/caspel26/hinoki"><img alt="Coverage" src="https://codecov.io/gh/caspel26/hinoki/branch/main/graph/badge.svg?labelColor=1d1a24"></a>
  <img alt="Python 3.12, 3.13 and 3.14" src="https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-7a55c4?labelColor=1d1a24">
  <a href="https://caspel26.github.io/hinoki/dev/status/"><img alt="Status: pre-alpha" src="https://img.shields.io/badge/status-pre--alpha-e3c29b?labelColor=1d1a24"></a>
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-7a55c4?labelColor=1d1a24"></a>
</p>

<p align="center">
  <img src="assets/readme/check.svg" width="860" alt="hinoki check reports three rule violations, each with its file and line, a rule ID and a fix, then a summary: 2 rules, 3 violated">
</p>

> [!WARNING]
> **Pre-alpha.** The API and the migration format may change, and Hinoki is not published on PyPI. Use a source checkout to evaluate it, with disposable data; do not run it in production.

## What Hinoki is

Hinoki is a Python framework for HTTP APIs, built around three ideas.

| Declare | Check | Run |
| --- | --- | --- |
| Each endpoint states its types, who may call it, whether it runs in a transaction and which errors it returns. The same declaration drives validation, responses and the OpenAPI document. | You choose rules such as "every write runs in a transaction" or "the models never import the API layer". `hinoki check` reports a broken rule with its file, line and fix, and fails. | The app runs as ASGI under Granian. Its adapter and handlers run in Python; selected routing, parsing, schema, cryptography and PostgreSQL/SQLite ORM work uses the Rust core. |

## What is in the box

| Area | What you get |
| --- | --- |
| **Endpoints** | `@http.get`, `post`, `put`, `patch`, `delete` and `query` on functions, route groups or classes, with typed path, query and body parameters |
| **Schemas** | Strict validation without type coercion, response shaping, and schemas derived from a model |
| **Access and errors** | Reusable access policies with permissions; declared errors that appear in OpenAPI |
| **SQL ORM** | PostgreSQL and SQLite models, relations loaded only when you ask, querysets, transactions, and migration files that you review and apply yourself |
| **Architecture rules** | `hinoki check` in a terminal or in CI, with waivers that must name a reason and an owner and can expire |
| **Settings and modules** | Typed settings from the environment, files or TOML; per-worker resources with startup and shutdown |
| **Tools** | OpenAPI with Swagger UI and ReDoc, in-process test clients, a project scaffold, an MCP server for coding agents, and an optional admin |

## A small API

The [Notes example](examples/notes) is a complete application in two short files. The model, the schemas and two of its endpoints, from [`examples/notes/app.py`](examples/notes/app.py):

```python
from typing import Annotated
from uuid import UUID, uuid4

from hinoki import Field, Schema, http
from hinoki.orm import Model, field, indexed, ordered_by


@indexed("title")
@ordered_by("title")
class Note(Model, table="notes"):
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


@http.get("/notes")
async def listing() -> list[NoteOut]:
    return [NoteOut.from_model(n) for n in await Note.all()]


@http.post("/notes", status=201, transaction=True)
async def create(data: NoteIn) -> NoteOut:
    return NoteOut.from_model(await Note.acreate(data))
```

The application, from [`examples/notes/main.py`](examples/notes/main.py):

```python
from examples.notes.app import create, listing, one, remove
from hinoki import Hinoki, settings
from hinoki.db import DatabaseModule


class NotesSettings(settings.AppSettings, prefix="NOTES_"):
    database: settings.DatabaseSettings


app = Hinoki(
    settings=NotesSettings,
    title="Notes",
    openapi_url="/openapi.json",
    docs_url="/docs",
    include=[listing, one, create, remove],
    modules=[DatabaseModule],
)
```

The model says what is stored. The schemas say what may come in and what goes out, so a column never leaves the application unless a schema names it. `DatabaseModule` opens a connection pool when a worker starts and closes it when the worker stops; nothing connects at import.

## OpenAPI and interactive API docs

Hinoki generates an OpenAPI 3.1 document from registered routes, type annotations, request and response `Schema`s, declared errors, and authentication schemes. JSON, Swagger UI and ReDoc routes are opt-in:

```python
from hinoki import Hinoki

app = Hinoki(
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
)
```

The bundled viewers work without a CDN. See the [OpenAPI guide](docs/openapi.md) for document contents and current limits, including how the custom `QUERY` method is represented.

## Run it

You need [uv](https://docs.astral.sh/uv/) and a Rust toolchain (the core is built from source). Local settings use a project-local SQLite database by default; configure a disposable PostgreSQL URL when following this example.

```sh
git clone https://github.com/caspel26/hinoki
cd hinoki
uv sync --locked

export NOTES_DATABASE__URL=postgres://postgres:pw@127.0.0.1:5432/notes
export HINOKI_DATABASE_URL="$NOTES_DATABASE__URL"
```

Hinoki never creates tables by itself. Write the migration from the model, read it, then apply it:

```sh
uv run hinoki migrations --dir examples/notes/migrations plan \
  --models examples.notes.app --name initial
uv run hinoki migrations --dir examples/notes/migrations sql 0001-initial
uv run hinoki migrations --dir examples/notes/migrations apply
```

Start the development server and send two requests from another terminal:

```sh
uv run hinoki dev examples.notes.main:app
```

```sh
curl -i -X POST http://127.0.0.1:8000/notes \
  -H 'content-type: application/json' \
  -d '{"title":"First note","body":"Hello"}'
curl http://127.0.0.1:8000/notes
```

The first answers `201 Created` with the stored note, the second returns the list. An empty title is refused before the handler runs:

```json
{"error":"validation_error","request_id":"…","details":[{"path":"data.title","code":"min_length","message":"at least 1 long"}]}
```

Swagger UI is at `http://127.0.0.1:8000/docs` and the OpenAPI document at `/openapi.json`. The [quick start](https://caspel26.github.io/hinoki/dev/quick-start/) explains each step, and the [tutorial](https://caspel26.github.io/hinoki/dev/tutorial/) builds a larger application.

## Rules you can run

The [Tracker example](examples/tracker) separates its API, its operations and its models, and states that separation as rules, in [`examples/tracker/rules.py`](examples/tracker/rules.py):

```python
from hinoki import Actions, Imports, Schemas, Transactions
from hinoki.rules import RuleSet

PKG = "examples.tracker"


tracker_rules = RuleSet(
    Actions.require_access(
        match="examples.tracker.*", except_=["examples.tracker.api.health"]
    ),
    Actions.forbid_model_responses(match=f"{PKG}.*"),
    # The MCP endpoint dispatches other actions, which have their own transactions.
    Transactions.require_atomic(except_=["hinoki.mcpapp.*"]),
    Schemas.forbid_exposure("token"),
    # The direction of dependencies: api -> operations -> models.
    Imports.forbid(
        source="examples.tracker.models", target="examples.tracker.operations"
    ),
    Imports.forbid(source=f"{PKG}.models", target=f"{PKG}.api"),
    Imports.forbid(source=f"{PKG}.operations", target=f"{PKG}.api"),
)
```

```sh
TRACKER_DATABASE_URL=postgres://unused uv run hinoki check examples.tracker.app:create_app \
  --rules examples.tracker.rules:tracker_rules --package examples.tracker
```

```text
7 rule(s): 0 violated, 0 unverifiable, 0 waived
```

The check reads the application and its source; it does not connect to the database. When a promise is broken it says where and how to fix it, and exits with status 1: the output at the top of this page is the real result for a small application in the test suite ([`tests/projects/rulesapp.py`](tests/projects/rulesapp.py)) that breaks two rules on purpose.

Rules are optional. An application without a rule set runs the same way; see the [rules guide](https://caspel26.github.io/hinoki/dev/rules/).

## Access and transactions, stated once

A route group carries the policy for all of its endpoints. From [`examples/tracker/api.py`](examples/tracker/api.py):

```python
from uuid import UUID

from examples.tracker import operations
from examples.tracker.errors import TaskAlreadyDone
from examples.tracker.models import Member, Task
from examples.tracker.schemas import TaskOut
from examples.tracker.security import member_only
from hinoki import Caller, RouteGroup, errors


def out(task: Task) -> TaskOut:
    return TaskOut.from_model(task)


tasks = RouteGroup(
    "/tasks", access=member_only, atomic=True
)  # stated once for the group


@tasks.post("/{task_id}/complete", errors=(errors.NotFound, TaskAlreadyDone))
async def complete_task(task_id: UUID, caller: Caller[Member]) -> TaskOut:
    return out(await operations.complete_task(caller.user.id, task_id))
```

`hinoki routes` prints what the application really serves:

```text
GET     /health                    public                   examples.tracker.api.health
POST    /mcp                       MemberTokens[tasks:use]  hinoki.mcpapp.expose_as_mcp.<locals>.mcp_endpoint
GET     /tasks                     MemberTokens[tasks:use]  examples.tracker.api.list_tasks
POST    /tasks                     MemberTokens[tasks:use]  examples.tracker.api.create_task
DELETE  /tasks/{task_id}           MemberTokens[tasks:use]  examples.tracker.api.delete_task
POST    /tasks/{task_id}/complete  MemberTokens[tasks:use]  examples.tracker.api.complete_task
```

## Start your own project

```sh
uv run hinoki new shop --dir ..
```

This writes `../shop` with an application, a rule set, a test, settings, and instructions and an MCP configuration for coding agents. Its `pyproject.toml` points at this checkout, because Hinoki is not on PyPI. The [AI workflow guide](https://caspel26.github.io/hinoki/dev/ai-workflow/) describes how an agent can use the development MCP server.

## Admin

The optional admin (`hinoki.admin`) gives staff lists, search, forms, bulk actions and a dashboard for the models you register, behind the same access policies as the API. It has four built-in styles, each in light and dark. The [Blog example](examples/blog) uses it.

| Studio | Console |
| --- | --- |
| <picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/admin-studio-list-dark.png"><img src="docs/screenshots/admin-studio-list-light.png" alt="Studio admin style: a list page"></picture> | <picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/admin-console-list-dark.png"><img src="docs/screenshots/admin-console-list-light.png" alt="Console admin style: a list page"></picture> |

| Paper | Bento |
| --- | --- |
| <picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/admin-paper-list-dark.png"><img src="docs/screenshots/admin-paper-list-light.png" alt="Paper admin style: a list page"></picture> | <picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/admin-bento-list-dark.png"><img src="docs/screenshots/admin-bento-list-light.png" alt="Bento admin style: a list page"></picture> |

## Status and limits

Hinoki is pre-alpha and has no release. What exists is tested in CI on Python 3.12, 3.13 and 3.14 on Linux. The limits that matter most today:

- SQLite is the local-development default; PostgreSQL is also supported. Other SQL backends are planned but not supported yet.
- The server path is ASGI on [Granian](https://github.com/emmett-framework/granian); a WSGI adapter exists and is verified with Granian only. Streaming responses and WebSockets are not available.
- There is no general CORS middleware and no cursor pagination.
- Many-to-many relations use an explicit join model.
- Type checkers verify model construction, but not every keyword-based ORM call.
- There are no end-to-end performance claims. One measured comparison, with its method and limits, is on the [benchmarks page](https://caspel26.github.io/hinoki/dev/benchmarks/).

The complete list is on the [status page](https://caspel26.github.io/hinoki/dev/status/).

## Documentation

The [documentation site](https://caspel26.github.io/hinoki/) has the tutorial, the guides, the API reference, the settings and the CLI. It follows the `main` branch until the first release. To build it locally, run `uv run --locked --group docs mkdocs serve`.

For the project's direction see the [roadmap](ROADMAP.md); to work on Hinoki itself see the [contributing guide](CONTRIBUTING.md). Hinoki is released under the [MIT license](LICENSE).
