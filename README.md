<p align="center">
  <a href="https://caspel26.github.io/hinoki/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="assets/readme/lockup-dark.svg">
      <source media="(prefers-color-scheme: light)" srcset="assets/readme/lockup-light.svg">
      <img src="assets/readme/lockup-light.svg" alt="Hinoki" width="340">
    </picture>
  </a>
</p>

<h3 align="center">Python APIs with architecture you can check.</h3>

<p align="center">
  <a href="https://github.com/caspel26/hinoki/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/caspel26/hinoki/ci.yml?branch=main&label=CI&labelColor=1d1a24"></a>
  <a href="https://codecov.io/gh/caspel26/hinoki"><img alt="Coverage" src="https://codecov.io/gh/caspel26/hinoki/branch/main/graph/badge.svg?labelColor=1d1a24"></a>
  <img alt="Python 3.12, 3.13 and 3.14" src="https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-7a55c4?labelColor=1d1a24">
  <a href="https://caspel26.github.io/hinoki/dev/status/"><img alt="Status: pre-alpha" src="https://img.shields.io/badge/status-pre--alpha-e3c29b?labelColor=1d1a24"></a>
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-7a55c4?labelColor=1d1a24"></a>
</p>

<p align="center">
  <a href="https://caspel26.github.io/hinoki/"><strong>Documentation</strong></a> ·
  <a href="https://caspel26.github.io/hinoki/dev/quick-start/">Quick start</a> ·
  <a href="https://caspel26.github.io/hinoki/dev/tutorial/">Tutorial</a> ·
  <a href="examples">Examples</a> ·
  <a href="https://caspel26.github.io/hinoki/dev/status/">Status</a>
</p>

Hinoki is a framework for HTTP APIs: handlers in Python, a core in Rust. An endpoint declares its types, who may call it, whether it runs in a transaction and which errors it returns. That one declaration drives routing, validation, the OpenAPI document, and the optional rules that `hinoki check` runs in CI.

> [!WARNING]
> **Pre-alpha, not released.** The API and migration format may change. The [`hinoki` package on PyPI](https://pypi.org/project/hinoki/) is unrelated: evaluate this one from a source checkout, with disposable data.

<table>
  <tr>
    <td width="33%" valign="top"><strong>Declared</strong><br>Access, transactions and errors are options of the decorator or defaults of a route group. Access and errors also land in OpenAPI.</td>
    <td width="33%" valign="top"><strong>Checked</strong><br>Five optional rules: access, transactions, model responses, exposed fields, imports. Each finding has a file, line and fix; waivers need an owner and a reason.</td>
    <td width="33%" valign="top"><strong>Strict</strong><br>Input is validated without coercion and unknown fields are refused. A response carries only what its <code>Schema</code> names.</td>
  </tr>
  <tr>
    <td valign="top"><strong>Explicit</strong><br>Nothing connects at import. Tables change only through SQL migration files you review and apply.</td>
    <td valign="top"><strong>Rust core</strong><br>Path matching, selected header and query parsing, validation, serialization, crypto and SQL run in a compiled extension. Handlers stay Python, sync or async.</td>
    <td valign="top"><strong>Included</strong><br>ORM for SQLite, PostgreSQL, MySQL and MariaDB, generated CRUD, typed settings, test clients, MCP and an admin.</td>
  </tr>
</table>

## A complete app

The [Notes example](examples/notes), condensed to one file:

```python
from typing import Annotated
from uuid import UUID, uuid4

from hinoki import Field, Hinoki, Schema, http, settings
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


class NotesSettings(settings.AppSettings, prefix="NOTES_"):
    database: settings.DatabaseSettings


app = Hinoki(
    settings=NotesSettings,
    include=[create],
    openapi_url="/openapi.json",
    docs_url="/docs",
)
```

Invalid input is answered `422` before the handler runs:

```json
{"error": "validation_error", "request_id": "…", "details": [{"path": "data.title", "code": "min_length", "message": "at least 1 character"}]}
```

## Try it

You need [uv](https://docs.astral.sh/uv/) and a Rust toolchain; the core builds from source.

```sh
git clone https://github.com/caspel26/hinoki && cd hinoki
uv sync --locked
cd examples   # three example apps; the environment is one level up
uv run --project .. hinoki migrations sql --app notes 0001-create-table-notes   # review it
uv run --project .. hinoki migrations apply --app notes                         # local SQLite
uv run --project .. hinoki dev notes.main:app
```

Open <http://127.0.0.1:8000/docs> for Swagger UI. To start your own project, run `hinoki new NAME`.

## Access and transactions, stated once

From the [Tracker example](examples/tracker), condensed:

```python
from uuid import UUID

from hinoki import Caller, RouteGroup, errors
from tracker import operations
from tracker.errors import TaskAlreadyDone
from tracker.models import Member, Task
from tracker.schemas import TaskOut
from tracker.security import member_only


def out(task: Task) -> TaskOut:
    return TaskOut.from_model(task)


tasks = RouteGroup("/tasks", access=member_only, atomic=True)  # writes run in a transaction


@tasks.post("/{task_id}/complete", errors=(errors.NotFound, TaskAlreadyDone))
async def complete_task(task_id: UUID, caller: Caller[Member]) -> TaskOut:
    return out(await operations.complete_task(caller.user.id, task_id))
```

The policy and the declared `404` and `409` are in the OpenAPI document, and `hinoki routes` prints who may call each route.

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

`hinoki check` reads the app and its source without a database and exits `1` on a violation; `--format json` gives CI and editors the same findings. Pass `rules=` to `Hinoki` to enforce them at startup too.

<p align="center">
  <img src="assets/readme/check.svg" width="760" alt="hinoki check reports three rule violations, each with its file and line, a rule ID and a fix, then a summary: 2 rules, 3 violated">
</p>

## Admin

Optional, and absent until included. The admin provides staff lists, search, forms, bulk actions and dashboards behind your `Access` policy, with CSRF-protected forms. It does not provide a sign-in page; the [Blog example](examples/blog/adminsite.py) authenticates staff with its application policy:

```python
from blog.adminsite import build_admin
from blog.settings import BlogSettings

admin_settings = BlogSettings.load()
app.include(build_admin(
    admin_settings.admin_secret.reveal(), admin_settings.admin_theme
).api())
```

<table>
  <tr>
    <td width="50%" align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/admin-studio-list-dark.png"><img src="docs/screenshots/admin-studio-list-light.png" alt="Studio admin style: a list of blog posts"></picture><br><sub><strong>Studio</strong></sub></td>
    <td width="50%" align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/admin-console-list-dark.png"><img src="docs/screenshots/admin-console-list-light.png" alt="Console admin style: a list of blog posts"></picture><br><sub><strong>Console</strong></sub></td>
  </tr>
  <tr>
    <td align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/admin-paper-list-dark.png"><img src="docs/screenshots/admin-paper-list-light.png" alt="Paper admin style: a list of blog posts"></picture><br><sub><strong>Paper</strong></sub></td>
    <td align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/admin-bento-list-dark.png"><img src="docs/screenshots/admin-bento-list-light.png" alt="Bento admin style: a grid of blog post cards"></picture><br><sub><strong>Bento</strong></sub></td>
  </tr>
</table>

## OpenAPI and databases

Hinoki generates an OpenAPI 3.1 document from routes, schemas, declared errors and authentication. Enable the JSON document and bundled Swagger UI or ReDoc with `openapi_url`, `docs_url` and `redoc_url`; see the [OpenAPI guide](docs/openapi.md). Database settings default to local SQLite and can select PostgreSQL, MySQL or MariaDB with a URL or typed connection fields. Hinoki opens a pool per worker and never runs migrations at startup; see [database configuration](docs/databases.md).

## Python handlers, Rust core

The compiled extension handles path matching, selected query, header and cookie parsing, schema validation and serialization, cryptography, and ORM SQL compilation and execution. The ASGI adapter, handler calls and most of the request pipeline run in Python. Apps use Granian by default or the opt-in Rust HTTP server (`--server rust`).

## Status

Tested in CI on Linux with Python 3.12 to 3.14. Not available yet: SQL Server and Oracle, streaming responses and WebSockets, cursor pagination, per-route CORS. The [status page](https://caspel26.github.io/hinoki/dev/status/) lists every current boundary.

<p align="center">
  <a href="https://caspel26.github.io/hinoki/dev/guides/">Guides</a> ·
  <a href="https://caspel26.github.io/hinoki/dev/reference/">Reference</a> ·
  <a href="examples">Examples</a> ·
  <a href="ROADMAP.md">Roadmap</a> ·
  <a href="CONTRIBUTING.md">Contributing</a> ·
  <a href="LICENSE">MIT license</a>
</p>
