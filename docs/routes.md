---
status: implemented
related:
  - function-endpoints.md
  - class-endpoints.md
  - tutorial/8-projects.md
  - cli.md
---

# Register routes

An application serves the route map that `[project] routes` in `hinoki.toml` points to. Registering an app in `hinoki.toml` and exposing its HTTP routes in the project's `routes.py` are two separate decisions.

## From the manifest to the application

```toml
# hinoki.toml
[project]
application = "notes.main:app"
routes = "notes.routes:routes"            # the project's single route map

[[apps]]
name = "billing"
models = "notes.apps.billing.models"
migrations = "notes.apps.billing.migrations"
requires = []
```

```python
# notes/routes.py
from hinoki import Includable, Mount

from notes.api import health
from notes.apps.billing.routes import routes as billing_routes

routes: list[Includable] = [
    health,
    Mount("/billing", billing_routes),
]
```

```python
# notes/main.py
from hinoki import Hinoki

from notes.settings import ProjectSettings

app = Hinoki(
    settings=ProjectSettings,
    title="Notes",
    openapi_url="/openapi.json",
    docs_url="/docs",
)  # routes, rules, events and middleware come from hinoki.toml
```

When the module named by `project.application` builds `Hinoki(...)` without `include=`, it loads `[project] routes` automatically (`[]` if omitted). Passing `include=` explicitly (even `include=[]`) always wins over the manifest, which is useful for tests or conditional routes. `manifest_routes()` reads `[project] routes` explicitly for scripts or separate modules, and raises `ProjectError` outside a project.

`hinoki app create billing` scaffolds `notes/apps/billing/routes.py` and prints the `Mount("/billing", billing_routes)` snippet to add to `notes/routes.py` if you want to expose the app over HTTP. An app registered in `hinoki.toml` without a `Mount(...)` in `notes/routes.py` still participates in migrations, events and `requires` without serving HTTP endpoints; an API-only app created with `hinoki app create gateway --no-models` (or `hinoki app add gateway`) has no models or migrations and is mounted in `notes/routes.py` like any other app.

## One place for each prefix

| Segment | Declared by | Example |
| --- | --- | --- |
| The app | `Mount(...)` in the project's `routes.py` | `/billing` |
| A version inside the app | `Mount` in the app's `routes.py` | `/v2` |
| The resource | `RouteGroup(...)` or `API, path=...` | `/invoices` |

Write each segment once. Paths inside an app stay relative to the app, so the same app package can be mounted at different prefixes in different projects.

## Versions with `Mount`

`Mount(prefix, *items)` prepends its prefix to everything it holds. Use it inside `routes.py` when one app serves several versions:

```python
# notes/apps/billing/routes.py
from hinoki import Includable, Mount

from . import v1, v2

routes: list[Includable] = [
    Mount("/v1", v1.routes),
    Mount("/v2", v2.routes),
]
```

The result is `/billing/v1/invoices` and `/billing/v2/invoices`. Mounts nest, and their prefixes join from the outside in. A prefix starts with `/` and does not end with one, and a mount holds at least one item. Mounting changes the path only: access, errors and transactions stay as declared.

## What `include` accepts

`Includable` is the type of every item a routes list may hold:

| Item | Example |
| --- | --- |
| A decorated function | `health` |
| A `RouteGroup` | `invoices` |
| An `API` class, including a `@crud` class | `Invoices` |
| A `crud(...)` value | `crud(Invoice, path="/invoices", read=InvoiceOut)` |
| A `Mount` | `Mount("/v1", v1.routes)` |
| An object with an `api()` method | the admin |
| A list or tuple of these | `v1.routes` |

Nested lists are spread in place, in the order you wrote them. Annotate each list as `routes: list[Includable]` so the type checker rejects anything else; a module passed instead of its list is refused with a hint to export a list.

## The application module

Declare `app` once, at module level, in the module that `project.application` names. Importing it reads the settings and builds declarations; nothing connects until a worker starts, so commands, servers and tests import the same object. A factory target (`module:create_app`) is still accepted by the commands, for code that must build the application from runtime values.

## Routes for tests and options

To serve a subset, or the same routes with other settings, build another `Hinoki` with an explicit `include=`:

```python
# tests/billingroutestest.py
from hinoki import Hinoki, Mount

from notes.apps.billing.routes import routes

app = Hinoki(include=[Mount("/billing", routes)])
```

`include=[]` is an empty application. To add routes that exist only when a setting is on, call `app.include(...)` in the application module before it serves:

```python
# notes/main.py (continued)
from hinoki import Mount

from notes.apps.billing.exports import exports

if app.settings.billing_exports:
    app.include(Mount("/billing", exports))
```

## Inspect the map

```sh
hinoki routes
hinoki routes --path /billing --method POST
hinoki routes --format json
```

Rows are sorted by path, then method, and show the effective access policy and the handler. `--lanes` adds the serving lane of each route. See [list routes with `routes`](inspect.md#routes) for every option.

## Errors and edge cases

Everything below fails when the application is built, before it serves a request:

| Message | Cause |
| --- | --- |
| `cannot load the routes of app 'billing' (notes.apps.billing.routes:routes): …` | The module or attribute in `hinoki.toml` does not import |
| `the routes of app 'billing' (…) must be a list of routes` | `routes` is a single item, not a list or tuple |
| `RegistrationError` naming an action | Two routes reach the same method and path after prefixes join |
| `RegistrationError: application is already serving; it is frozen` | `app.include(...)` after the server started |

An `include` call registers all its items or none of them.
