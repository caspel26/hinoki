<p align="center">
  <img src="assets/hinoki-banner.png" alt="Hinoki" width="760">
</p>

<p align="center"><strong>Python APIs with architecture you can check.</strong></p>

<p align="center">
  <a href="https://github.com/caspel26/hinoki/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/caspel26/hinoki/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-b9a1f5">
  <img alt="Pre-alpha" src="https://img.shields.io/badge/status-pre--alpha-orange">
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-blue"></a>
</p>

> [!WARNING]
> **Pre-alpha:** APIs and migration formats may change. Hinoki is not published on PyPI. Use a source checkout for evaluation, not production.

Hinoki is a Python API framework with typed request and response schemas, explicit access and transaction policies, generated OpenAPI, optional architecture rules, and development tools. Selected request, schema, cryptography, and ORM paths run in Rust; application handlers run in Python.

## Try the examples

```sh
git clone https://github.com/caspel26/hinoki
cd hinoki
uv sync --locked
```

The [Notes example](examples/notes) is a small application. [Tracker](examples/tracker) shows layered services and architecture rules. [Blog](examples/blog) covers relations, authentication, the ORM, and the optional admin.

## A declared application

```python
from hinoki import DatabaseSettings, FrameworkSettings, Hinoki, get
from hinoki.db import DatabaseModule

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

Set `APP_DATABASE__URL`, then run `uv run hinoki dev package.main:app`. The database module opens a pool during each worker's startup and closes it during shutdown. A standalone script, such as the [blog seed command](examples/blog/seed.py), owns its short-lived connection directly.

Check the configured project rules with `hinoki check package.main:app`:

```text
7 rule(s): 0 violated, 0 unverifiable, 0 waived
```

## Models and schemas

Models declare database fields and indexes. Response schemas state which values leave the application.

```python
from uuid import UUID, uuid4

from hinoki.orm import Model, field, indexed, ordered_by

@indexed("title")
@ordered_by("title")
class Note(Model, table="notes"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str = field(max_length=80)
```

Hinoki uses decorators for model options. See [model options](docs/model-options.md) and the runnable [Notes app](examples/notes/app.py).

## Admin styles

The optional admin includes three built-in styles, each with light and dark modes.

| Studio | Console | Paper |
| --- | --- | --- |
| ![Studio admin](docs/screenshots/admin-studio-list-light.png) | ![Console admin](docs/screenshots/admin-console-list-light.png) | ![Paper admin](docs/screenshots/admin-paper-list-light.png) |

## Documentation

Read the [Hinoki documentation](https://caspel26.github.io/hinoki/) for the full tutorial, guides, API reference, settings, CLI, and current project boundaries. The same docs can be built locally with `uv run --locked --group docs mkdocs serve`.

For project direction and contribution instructions, see the [roadmap](ROADMAP.md) and [contributing guide](CONTRIBUTING.md).
