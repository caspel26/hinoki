<p align="center">
  <img src="assets/hinoki-banner.png" alt="Hinoki" width="760">
</p>

<p align="center"><strong>A Python API framework that checks its own architecture.</strong></p>

<p align="center">
  <a href="https://github.com/caspel26/hinoki/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/caspel26/hinoki/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-b9a1f5">
  <img alt="Pre-alpha" src="https://img.shields.io/badge/status-pre--alpha-orange">
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-blue"></a>
</p>

> [!WARNING]
> **Pre-alpha:** APIs may change and Hinoki is not published on PyPI. Use it for evaluation, not production.

Hinoki turns typed Python declarations into one contract shared by routing, validation, access policies, transactions, OpenAPI and development tools. Optional rules check architectural promises. Selected request, schema, crypto and ORM paths run in Rust. Hinoki does not build on Django, FastAPI, Starlette or Pydantic.

## Install from source

```sh
git clone https://github.com/caspel26/hinoki
cd hinoki
uv sync --locked
```

## Declare an application

```python
from hinoki import FrameworkSettings, Hinoki, get
from hinoki.db import DatabaseModule, DatabaseSettings

class AppSettings(FrameworkSettings, prefix="APP_"):
    database: DatabaseSettings

@get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

app = Hinoki(
    settings=AppSettings,
    include=[health],
    modules=[DatabaseModule],
)
```

Run it with `APP_DATABASE__URL=postgres://... uv run hinoki dev package.main:app`. `hinoki check package.main:app` runs the configured rules. The settings class and module declarations keep configuration and resource ownership explicit; see the [getting started guide](docs/getting-started.md).

## What Hinoki provides

- **Architecture you can check:** stable rule IDs, source locations and actionable fixes.
- **Declared policy:** `access=`, `transaction=True` and `errors=(...)` live beside the endpoint.
- **Development tooling:** `hinoki check`, project context and application MCP use the same live contracts.
- **Customizable by design:** handwritten and generated operations share policies and docs.
- **Explicit runtime:** typed settings, module dependencies and worker lifespans. Database pools open per worker at startup and close on shutdown; standalone scripts own their connection directly.

```text
$ TRACKER_DATABASE_URL=postgres://... uv run hinoki check examples.tracker.app:create_app
7 rule(s): 0 violated, 0 unverifiable, 0 waived
```

The [tracker example](examples/tracker) demonstrates layers, ownership rules and MCP. The [blog](examples/blog) demonstrates the ORM and optional admin.

Model-level ordering and indexes use decorators; there is no nested `class Meta`:

```python
from uuid import UUID, uuid4

from hinoki.orm import Model, field, indexed, ordered_by

@indexed("title")
@ordered_by("title")
class Note(Model, table="notes"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str = field(max_length=80)
```

See [model options](docs/MODEL-OPTIONS.md) and the runnable [notes example](examples/notes/app.py).

## Admin styles

One component system supports three distinct designs, each with light and dark modes.

| Studio | Console | Paper |
| --- | --- | --- |
| ![Studio admin](docs/screenshots/admin-studio-list-light.png) | ![Console admin](docs/screenshots/admin-console-list-light.png) | ![Paper admin](docs/screenshots/admin-paper-list-light.png) |

## Learn more

- [Documentation overview](docs/index.md) (build locally with `uv run --locked --group docs mkdocs serve`)
- [Tutorial](docs/tutorial/index.md)
- [Patterns and best practices](docs/PATTERNS.md)
- [Rules](docs/RULES.md)
- [Settings and secrets](docs/SETTINGS.md)
- [Current roadmap](ROADMAP.md)

Detailed supported features and known gaps live in the [documentation](https://caspel26.github.io/hinoki/), not in this project summary.
