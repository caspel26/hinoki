<p align="center">
  <a href="https://hinoki.caspel.dev/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="assets/readme/lockup-dark.svg">
      <img src="assets/readme/lockup-light.svg" alt="Hinoki" width="360">
    </picture>
  </a>
</p>

<p align="center">
  <strong>Typed Python APIs. Architecture rules you can run.</strong><br>
  <sub>High-throughput Python API framework powered by a Rust HTTP server and SQL ORM. One typed declaration drives routing, validation, OpenAPI, architecture rules, and MCP.</sub>
</p>

<p align="center">
  <a href="https://github.com/caspel26/hinoki/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/caspel26/hinoki/ci.yml?branch=main&label=CI&labelColor=1d1a24"></a>
  <a href="https://codecov.io/gh/caspel26/hinoki"><img alt="Coverage" src="https://codecov.io/gh/caspel26/hinoki/branch/main/graph/badge.svg?labelColor=1d1a24"></a>
  <a href="https://sonarcloud.io/project/overview?id=caspel26_hinoki"><img alt="Quality gate" src="https://sonarcloud.io/api/project_badges/measure?project=caspel26_hinoki&metric=alert_status"></a>
  <a href="https://github.com/caspel26/hinoki/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://img.shields.io/github/actions/workflow/status/caspel26/hinoki/codeql.yml?branch=main&label=CodeQL&labelColor=1d1a24"></a>
  <a href="https://scorecard.dev/viewer/?uri=github.com/caspel26/hinoki"><img alt="OpenSSF Scorecard" src="https://api.scorecard.dev/projects/github.com/caspel26/hinoki/badge"></a>
  <a href="https://www.bestpractices.dev/projects/16941"><img alt="OpenSSF Best Practices" src="https://www.bestpractices.dev/projects/16941/badge"></a>
  <img alt="Python 3.12, 3.13 and 3.14" src="https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-7a55c4?labelColor=1d1a24">
  <a href="https://hinoki.caspel.dev/dev/status/"><img alt="Status: pre-alpha" src="https://img.shields.io/badge/status-pre--alpha-e3c29b?labelColor=1d1a24"></a>
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-7a55c4?labelColor=1d1a24"></a>
</p>

<p align="center">
  <a href="https://hinoki.caspel.dev/dev/">Documentation</a> ·
  <a href="https://hinoki.caspel.dev/dev/quick-start/">Quick start</a> ·
  <a href="https://hinoki.caspel.dev/dev/tutorial/">Tutorial</a> ·
  <a href="https://hinoki.caspel.dev/dev/benchmarks/">Benchmarks</a> ·
  <a href="examples/README.md">Examples</a> ·
  <a href="https://hinoki.caspel.dev/dev/status/">Status</a>
</p>

<p align="center">
  <picture>
    <source media="(prefers-reduced-motion: reduce)" srcset="assets/readme/demo.png">
    <img src="assets/readme/demo.apng" alt="In a terminal window, hinoki new creates the blog project and the posts app; hinoki check flags a write route missing atomic=True with its file, line and fix (HNK-TXN-001), applies the fix and passes with 0 violated rules; hinoki dev starts Kiso with its docs URL, curl POST /posts returns 201 Created, and curl to /docs returns 200." width="840">
  </picture>
</p>
<p align="center"><sub>Real terminal output recorded by <a href="scripts/readme_demo.py"><code>scripts/readme_demo.py</code></a>; waits are shortened, and the posts app's source files and migrations are written off screen.</sub></p>

> [!WARNING]
> **Pre-alpha, not released.** In the planned `0.1.x` line, documented Python, CLI, settings and runtime contracts, plus migration files, stay compatible across patch releases. Additions and fixes for incorrect behavior remain possible and will be described in the changelog. Breaking changes may arrive in a later `0.y.0`; there is no fixed deprecation period and no compatibility promise for undocumented internals or old framework versions. Review migrations and use disposable data when evaluating Hinoki. The package will be named `hinoki-api`; the [`hinoki` package on PyPI](https://pypi.org/project/hinoki/) is an unrelated project.

Hinoki is a high-throughput Python API framework powered by a Rust HTTP server and SQL ORM. Apps run on **Kiso**, Hinoki's own HTTP server, written in Rust on hyper, tokio and rustls. `hinoki dev` and `hinoki serve` run Kiso by default, and `--server granian` runs [Granian](https://github.com/emmett-framework/granian) instead. One typed declaration drives routing, strict schema validation and serialization, OpenAPI, architecture rules, and MCP. You declare schemas, access policies and `atomic` writes in plain Python; `hinoki check` turns architecture rules into findings with a file, a line and a fix. Projects are agent-ready out of the box, with built-in application and development MCP servers and a generated `AGENTS.md`.

<p align="center">
  <a href="#enforce-architecture-with-hinoki-check">hinoki check</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#write-an-api">Write an API</a> ·
  <a href="#what-you-get">What you get</a> ·
  <a href="#interactive-api-docs">API docs</a> ·
  <a href="#admin">Admin</a> ·
  <a href="#python-and-rust">Python and Rust</a> ·
  <a href="#status">Status</a>
</p>

## Enforce architecture with `hinoki check`

A project declares its architecture contracts in plain Python (`rules.py`), and `hinoki check` verifies every endpoint against them before the server starts ([rules guide](docs/rules.md)):

```python
# notes/rules.py
from hinoki import rules

project_rules = rules.RuleSet(
    rules.Actions.require_access(match="notes.*", except_=["notes.api.health"]),
    rules.Actions.require_permissions(match="notes.*", except_=["notes.api.health"]),
    rules.Transactions.require_atomic(),
)
```

These **project rules** set the invariants for the whole codebase; on individual endpoints, the author satisfies them with explicit options such as `access=staff` and `atomic=True` on `@http.*`, `RouteGroup`, or `crud(...)`. If a write route omits `atomic=True`:

```python
# notes/apps/billing/api.py
@http.post("/invoices", access=staff, status=201)
async def create_invoice(data: InvoiceIn) -> InvoiceOut:
    return InvoiceOut(customer=data.customer)
```

`hinoki check` fails with the rule ID, file, line, and fix:

```console
$ hinoki check
notes/apps/billing/api.py:26: HNK-TXN-001 [violated] POST /billing/invoices (create_invoice) does not declare atomic=True
  fix: add atomic=True to the action (or declare it on a RouteGroup(..., atomic=True) group)
8 rule(s): 1 violated, 0 unverifiable, 0 waived
```

Adding `atomic=True` to `@http.post("/invoices", access=staff, atomic=True, status=201)` clears the finding:

```console
$ hinoki check
8 rule(s): 0 violated, 0 unverifiable, 0 waived
```

## How it works

You declare once. Routing, validation, the OpenAPI document, rules and the development tools read the same declaration; table changes follow a separate, explicit lane.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/readme/arch-dark.svg">
    <img src="assets/readme/arch-light.svg" alt="Architecture: Model, Schema and Endpoint declarations form one declared contract, which routing, validation, access and transactions, the OpenAPI document, hinoki check rules and the development MCP all read. Table changes follow a separate lane: migrations plan, a reviewed SQL file, an explicit apply, then the database." width="860">
  </picture>
</p>

Every request goes through the same steps, in this order. Validation, access and the transaction fail in different ways, so the order is part of the contract ([request lifecycle](docs/request-lifecycle.md)).

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/readme/flow-dark.svg">
    <img src="assets/readme/flow-light.svg" alt="Request flow: the server accepts the request, Hinoki routes it, checks access, binds and validates the input, opens a transaction, calls the handler, renders the response and commits. On an error an open transaction rolls back first, and the error response carries a request ID." width="860">
  </picture>
</p>

The router, request parsing and schema validation run in the Rust extension; your handler is plain Python. On an error, an open transaction rolls back before the error response is produced, and every response carries a request ID.

## Quick start

You need [uv](https://docs.astral.sh/uv/) and a stable Rust toolchain; the native extension builds from source.

```sh
git clone https://github.com/caspel26/hinoki && cd hinoki
uv sync --locked
source .venv/bin/activate
hinoki new notes --dir ..   # asks for the database, the server, the rest and a first app
cd ../notes
hinoki dev                  # API docs at http://127.0.0.1:8000/docs
```

The [Quick start](docs/quick-start.md) walks through the prompts.

## Declare and run an app

`hinoki new` writes the application as a module-level `app` in `notes/main.py`, titled after the project:

```python
# notes/main.py
from hinoki import Hinoki

from notes.settings import ProjectSettings

app = Hinoki(
    settings=ProjectSettings,  # NOTES_* variables and .env
    title="Notes",
    openapi_url="/openapi.json",
    docs_url="/docs",
)  # routes, rules and middleware come from hinoki.toml
```

`hinoki.toml` names that application, its project route map and its rules, so commands inside the project take no target:

```toml
[project]
application = "notes.main:app"
routes = "notes.routes:routes"
rules = ["notes.rules:project_rules"]
```

`Hinoki(settings=ProjectSettings, ...)` reads `hinoki.toml` and loads `[project].routes` (`notes.routes:routes`) automatically, along with `rules` and any `middleware`. Loading the project route map is separate from mounting individual apps: `notes/routes.py` starts with `health` (`/health`) and decides which apps are exposed over HTTP via `Mount(...)` ([middleware](docs/middleware.md), [rules](docs/rules.md)).

```sh
hinoki dev      # serve on Kiso, reload on changes
hinoki check    # run the project's rules
hinoki routes   # list every route with its access policy
```

## Write an API

The project package holds no models. Answer yes to the last question of `hinoki new` (or pass `--app NAME`), or create an app later; `hinoki app create` registers the app's models, migrations and dependencies in `hinoki.toml` and generates an initially empty route list (`routes: list[Includable] = []` in `notes/apps/billing/routes.py`):

```sh
hinoki app create billing
```

Mount that app's route list in `notes/routes.py`, preserving `/health` and adding `Mount("/billing", billing_routes)`:

```python
# notes/routes.py
from hinoki import Includable, Mount

from notes.api import health
from notes.apps.billing.routes import routes as billing_routes

routes: list[Includable] = [health, Mount("/billing", billing_routes)]
```

Once you add endpoints to `notes/apps/billing/routes.py`, the project exposes them under `/billing` through that `Mount`. Verify the active route table with `hinoki routes`:

```console
$ hinoki routes
POST    /billing/invoices  Tokens[billing:write]    notes.apps.billing.api.create_invoice
GET     /health            public                   notes.api.health
```

Inside an app, you declare models, `Schema` classes at the API boundary and list filters:

```python
# notes/apps/notes/models.py
from uuid import UUID, uuid4

from hinoki import db
from hinoki.orm import Model, field, pk


@db.table("notes")
class Note(Model):
    id: UUID = pk(default_factory=uuid4)
    title: str = field(max_length=80, index=True)
    body: str = field(max_length=2000, default="")


@db.table("notes_editors")
class Editor(Model):
    id: UUID = pk(default_factory=uuid4)
    token_hash: str = field(unique=True)  # SHA-256 hex digest of the token
```

```python
# notes/apps/notes/schemas.py
from typing import Annotated
from uuid import UUID

from hinoki import Field, Schema, derive
from hinoki.filters import Eq, FilterSet, Search

from notes.apps.notes.models import Note


class NoteIn(Schema):
    title: Annotated[str, Field(min_length=1, max_length=80)]
    body: Annotated[str, Field(max_length=2000)] = ""


@derive(Note, only=("title", "body"), partial=True)
class NotePatch(Schema): ...


class NoteOut(Schema):
    id: UUID
    title: str
    body: str


class NoteStats(Schema):
    total: int


class NoteFilters(FilterSet):
    title: Eq[str]
    search = Search("title", "body")
    ordering = ("title",)
```

An `Access` policy pairs a token check with the permissions it requires ([Authentication](docs/authentication.md)); `crud` generates the actions, applies the policy to each and wraps writes in a transaction:

```python
# notes/apps/notes/security.py
import hashlib

from hinoki import Access, BearerAuth, Caller, Permission

from notes.apps.notes.models import Editor


class NotePermission(Permission):
    WRITE = "notes:write"


class EditorTokens(BearerAuth[Editor]):
    async def resolve_token(self, token: str) -> Caller[Editor] | None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        editor = await Editor.aget_or_none(token_hash=digest)
        if editor is None:
            return None
        return Caller(
            id=str(editor.id), user=editor, permissions={NotePermission.WRITE}
        )


editors = Access(auth=EditorTokens(), permissions=[NotePermission.WRITE])
```

```python
# notes/apps/notes/routes.py
from hinoki import Includable, crud

from notes.apps.notes.api import stats
from notes.apps.notes.models import Note
from notes.apps.notes.schemas import NoteFilters, NoteIn, NoteOut, NotePatch
from notes.apps.notes.security import editors

routes: list[Includable] = [
    crud(
        Note,
        path="",
        read=NoteOut,
        create=NoteIn,
        update=NotePatch,
        delete=True,
        access=editors,
        filters=NoteFilters,
    ),
    stats,
]
```

Anything that is not CRUD is a plain function with an `@http` decorator (`@http.get`, `@http.post`, `@http.put`, `@http.patch`, `@http.delete`), taking the same `access` and `atomic` options:

```python
# notes/apps/notes/api.py
from hinoki import http

from notes.apps.notes.models import Note
from notes.apps.notes.schemas import NoteStats
from notes.apps.notes.security import editors


@http.get("/stats", access=editors)
async def stats() -> NoteStats:
    return NoteStats(total=await Note.all().acount())
```

`notes/apps/notes/routes.py` lists the generated `crud(...)` value and `stats`, and `notes/routes.py` mounts them at `/notes` with `Mount("/notes", ...)`; `main.py` does not change.

```console
$ hinoki routes
GET     /health           public                    notes.api.health
GET     /notes            EditorTokens[notes:write] notes.apps.notes.routes.NoteCrud.list
POST    /notes            EditorTokens[notes:write] notes.apps.notes.routes.NoteCrud.create
GET     /notes/stats      EditorTokens[notes:write] notes.apps.notes.api.stats
DELETE  /notes/{item_id}  EditorTokens[notes:write] notes.apps.notes.routes.NoteCrud.delete
GET     /notes/{item_id}  EditorTokens[notes:write] notes.apps.notes.routes.NoteCrud.retrieve
PATCH   /notes/{item_id}  EditorTokens[notes:write] notes.apps.notes.routes.NoteCrud.update
```

The list accepts `?title=...`, `?search=...` and `?ordering=title` from the `FilterSet`, and pages by number. Input is validated before the handler runs and a response contains only its declared fields. Tables change only through reviewed SQL: `hinoki migrations plan notes` writes the migration, `hinoki migrations apply` runs it. For several handwritten actions, group the `@http` functions on a `RouteGroup`, which shares a path, an access policy and a transaction across its actions, or use a class-based `API` with `crud` hooks ([function-based APIs](docs/function-endpoints.md), [CRUD](docs/crud.md), [class-based APIs](docs/class-endpoints.md)).

## What you get

| Area | What it gives you |
| --- | --- |
| **Contracts** | `Schema` input and output, access policies, `atomic` writes and declared errors drive validation, the OpenAPI document and the test client. Generated lists take client filters, ordering and search from a `FilterSet`. |
| **Rules** | `hinoki check` has twelve built-in rules: access, permissions, return types, pagination, transactions, model responses, exposed fields, string limits, pinned tables, foreign-key indexes, import direction and, when you list it, email templates that match their context. A new project turns on eight. Each finding names a file, a line and a fix; a waiver needs an owner and a reason. |
| **Data** | SQLite, PostgreSQL, MySQL and MariaDB, explicit relation loading, and SQL migration files you review. Importing an app never connects to a database. |
| **Tools** | Typed settings, generated CRUD, typed events, project-wide middleware, a CLI, MCP servers for the application and for development, `hinoki llms` / `llms.txt` and `AGENTS.md` support for AI agents, and a built-in admin. |

## Examples

Four self-contained projects in [`examples/`](examples/README.md), each with its own `hinoki.toml`, tests and a README with a reading order:

* [**Notes**](examples/notes/README.md) (easy): one app, one model and generated CRUD, in five minutes.
* [**Tracker**](examples/tracker/README.md) (medium): several apps with explicit dependencies, access policies, transactions, a filtered list, events, and project-wide middleware and rules.
* [**Blog**](examples/blog/README.md) (complete): tenant, login, admin, MCP, migrations, streaming, events, a rule waiver and metrics.
* [**Mailroom**](examples/mailroom/README.md) (focused): outbound email from TOML templates with typed contexts, an API-key policy, CLI preview and the `EmailTemplatesMatch` rule, with no database.

## Interactive API docs

With `docs_url="/docs"` (set by `hinoki new`), the app serves interactive API docs generated from the same declarations: paths, request bodies, responses and declared errors. This is the [Notes example](examples/notes/README.md); see the [OpenAPI guide](docs/openapi.md).

<p align="center">
  <img src="assets/readme/docs.png" alt="The interactive API docs of the Notes example, with POST /notes expanded to show its required JSON request body, with a title and a body, and its 201 response" width="75%">
</p>

## Admin

The optional admin is mounted explicitly, and installing or importing it never touches a database. See the [admin guide](docs/admin.md).

* **Access:** its own users, a sign-in page and per-model permissions by default; it can use your application's access policy instead.
* **Look:** four themes, each with light and dark modes.
* **Overview:** stat cards and recent records, as in the [Blog example](examples/blog/README.md).

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/readme/admin-login-dark.png">
    <img src="assets/readme/admin-login-light.png" alt="The admin sign-in page" width="32%">
  </picture>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/readme/admin-overview-dark.png">
    <img src="assets/readme/admin-overview-light.png" alt="The admin overview with stat cards and recent posts" width="63%">
  </picture>
</p>

## Python and Rust

Kiso is part of the native extension and has no ASGI boundary: eligible routes skip Python routing and binding, while Granian runs every request through the ASGI pipeline. Path matching, selected request parsing, schema validation and serialization, and ORM SQL compilation and execution run in Rust. On Kiso, an `async def` handler whose only awaits are ORM reads and stock writes (including guarded, body and `atomic` routes) is driven from Rust without an asyncio task, and eligible generated CRUD writes run their transaction in Rust; custom Python logic and middleware run in Python. [Serving](docs/serving.md) and [Kiso](docs/kiso.md) document the boundary.

## Status

Pre-alpha, tested in CI on Linux with Python 3.12–3.14. The [status page](https://hinoki.caspel.dev/dev/status/) lists what is not available yet.

The [benchmark report](docs/benchmarks.md) publishes retained measurements as evidence for the workloads tested — 5 stack setups across 6 request types (`health`, `json-1k`, `json-10k`, `list`, `detail`, `create`) backed by PostgreSQL 17.12 on one Linux host (`Intel Core i5-10300H`, 8 logical CPUs, 15 GiB RAM), at 100 concurrent connections with a 10 s warm-up and 2 server starts × 5 samples of 100,000 requests per scenario (10 samples per cell, or 20 when repeated for an unstable cell, with Kiso's `create` result marked unstable at 24.5% CV). Every stack passed a response-oracle check and SQL statement-count verification before measurement; the results cover warm loopback data on that machine and are not a general ranking or production claim.

<p align="center">
  <a href="https://hinoki.caspel.dev/dev/guides/">Guides</a> ·
  <a href="https://hinoki.caspel.dev/dev/reference/">Reference</a> ·
  <a href="examples/README.md">Examples</a> ·
  <a href="ROADMAP.md">Roadmap</a> ·
  <a href="CONTRIBUTING.md">Contributing</a> ·
  <a href="LICENSE">MIT license</a>
</p>
