<p align="center">
  <a href="https://caspel26.github.io/hinoki/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="assets/readme/lockup-dark.svg">
      <source media="(prefers-color-scheme: light)" srcset="assets/readme/lockup-light.svg">
      <img src="assets/readme/lockup-light.svg" alt="Hinoki" width="340">
    </picture>
  </a>
</p>

<h3 align="center">Typed Python APIs. Architecture rules you can run.</h3>

<p align="center">
  <a href="https://github.com/caspel26/hinoki/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/caspel26/hinoki/ci.yml?branch=main&label=CI&labelColor=1d1a24"></a>
  <a href="https://codecov.io/gh/caspel26/hinoki"><img alt="Coverage" src="https://codecov.io/gh/caspel26/hinoki/branch/main/graph/badge.svg?labelColor=1d1a24"></a>
  <img alt="Python 3.12, 3.13 and 3.14" src="https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-7a55c4?labelColor=1d1a24">
  <a href="https://caspel26.github.io/hinoki/dev/status/"><img alt="Status: pre-alpha" src="https://img.shields.io/badge/status-pre--alpha-e3c29b?labelColor=1d1a24"></a>
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-7a55c4?labelColor=1d1a24"></a>
</p>

<p align="center">
  <a href="https://caspel26.github.io/hinoki/dev/quick-start/">Quick start</a> ·
  <a href="https://caspel26.github.io/hinoki/dev/tutorial/">Tutorial</a> ·
  <a href="https://caspel26.github.io/hinoki/dev/benchmarks/">Benchmark report</a> ·
  <a href="examples">Examples</a> ·
  <a href="https://caspel26.github.io/hinoki/dev/status/">Status</a>
</p>

Hinoki is a pre-alpha Python framework for HTTP APIs, with a compiled Rust core. Handlers declare their input, output, access policy and transaction behavior. Those declarations feed request handling, the OpenAPI document and optional architecture rules.

> [!WARNING]
> **Not released.** The API and migration format may change. The [`hinoki` package on PyPI](https://pypi.org/project/hinoki/) is an unrelated legacy project. Evaluate this repository from a source checkout and use disposable data.

## Try the Notes API

You need [uv](https://docs.astral.sh/uv/) and a stable Rust toolchain. The native extension builds from source. These commands use the example's default local SQLite database:

```sh
git clone https://github.com/caspel26/hinoki
cd hinoki
uv sync --locked
cd examples
uv run --project .. hinoki migrations plan --app notes
uv run --project .. hinoki migrations sql --app notes 0001-create-table-notes
uv run --project .. hinoki migrations apply --app notes
uv run --project .. hinoki dev notes.main:app
```

Open <http://127.0.0.1:8000/docs> to inspect the generated OpenAPI UI. The full [Quick start](docs/quick-start.md) explains migration review, requests and database selection. To scaffold a project from the checkout, run `uv run hinoki new NAME`; its routes live in `routes.py`, which `hinoki.toml` points to, so `main.py` needs no `include=`. See [Installation](docs/getting-started.md).

## What the code looks like

This excerpt is from the [Notes example](examples/notes). The handler returns a model; the declared `NoteOut` type controls the response fields:

```python
from typing import Annotated
from uuid import UUID

from hinoki import Field, Schema, http
from .models import Note


class NoteIn(Schema):
    title: Annotated[str, Field(min_length=1, max_length=80)]
    body: str = ""


class NoteOut(Schema):
    id: UUID
    title: str
    body: str


@http.post("/notes", status=201, transaction=True)
async def create(data: NoteIn) -> NoteOut:
    return await Note.acreate(data)  # type: ignore[return-value]
```

Request values are validated before the handler runs. Unknown input fields are rejected by default; set `config=SchemaConfig(extra=SchemaExtra.IGNORE)` on a `Schema` to ignore them explicitly. A response exposes only its declared `Schema` fields. See [Schemas](docs/schemas.md) for configuration and error behavior.

## What Hinoki provides

- **Typed API declarations:** function and class endpoints, access policies, transactions, declared errors, generated CRUD, OpenAPI and test clients.
- **Architecture checks:** five optional rules cover access, write transactions, model responses, exposed fields and import direction. Findings include a source location and a fix; waivers need an owner and reason.
- **Database support:** the ORM supports SQLite, PostgreSQL, MySQL and MariaDB, with relations and reviewed SQL migration files. SQLite is the local-development default. Importing an app does not connect or change tables.
- **Tools:** typed settings, a CLI, application and development MCP integrations, and an optional admin mounted explicitly in the app.

The [Tracker example](examples/tracker) shows application layers, access control and rules. Its checks can run from `examples/` with the repository's locked environment:

```sh
cd examples
uv run --project .. hinoki check tracker.app:create_app
```

The [Blog example](examples/blog) demonstrates relations, authentication and the optional admin. The admin has four themes, uses the application's access policy and does not include a sign-in page. See the [admin guide](docs/admin.md) for setup and screenshots.

## Python and Rust boundary

The extension provides path matching, selected request parsing, schema validation and serialization, cryptography, and ORM SQL compilation and execution. The ASGI adapter, handlers and most request-pipeline work run in Python. Granian is the default server; an opt-in Rust HTTP server is available with `--server rust`. See [Serving](docs/serving.md) for the boundary and server limitations.

## Status and measurements

Hinoki is pre-alpha and is tested in CI on Linux with Python 3.12–3.14. SQL Server, Oracle, streaming responses, WebSockets, cursor pagination and per-route CORS are not available. The [status page](https://caspel26.github.io/hinoki/dev/status/) lists current boundaries.

The [benchmark report](docs/benchmarks.md) describes retained runs and their limits. Results apply only to the listed workloads, configurations and machine; they are not a general framework ranking or a production-readiness claim.

<p align="center">
  <a href="https://caspel26.github.io/hinoki/dev/guides/">Guides</a> ·
  <a href="https://caspel26.github.io/hinoki/dev/reference/">Reference</a> ·
  <a href="examples">Examples</a> ·
  <a href="ROADMAP.md">Roadmap</a> ·
  <a href="CONTRIBUTING.md">Contributing</a> ·
  <a href="LICENSE">MIT license</a>
</p>
