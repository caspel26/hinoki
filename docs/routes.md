---
status: implemented
related:
  - function-endpoints.md
  - class-endpoints.md
  - tutorial/8-projects.md
  - cli.md
---

# Register routes

An application serves the routes that `hinoki.toml` points to. The project and each app keep their routes in a `routes.py` that exports a list, and `notes/main.py` does not list them.

## From the manifest to the application

```toml
# hinoki.toml
[project]
application = "notes.main:app"
routes = "notes.routes:routes"            # the project's own routes

[[apps]]
name = "billing"
models = "notes.apps.billing.models"
migrations = "notes.apps.billing.migrations"
routes = "notes.apps.billing.routes:routes"
prefix = "/billing"                       # "/<name>" when omitted
requires = []
```

```python
# notes/main.py
from hinoki import Hinoki

from notes.rules import project_rules
from notes.settings import ProjectSettings

app = Hinoki(
    settings=ProjectSettings,
    title="Notes",
    openapi_url="/openapi.json",
    docs_url="/docs",
    rules=project_rules,
)
```

When the module named by `project.application` builds `Hinoki(...)` without `include=`, the application includes:

1. the project's `routes` list, at its own paths;
2. each app's `routes` list under the app's `prefix`, apps in dependency order (`requires`).

An app without a `routes` key, or with an empty list, adds nothing. `hinoki app create billing` writes the `routes.py` and the manifest keys; `hinoki app create billing --prefix /payments` chooses another prefix. `hinoki app list` prints each app with its routes and prefix.

## One place for each prefix

| Segment | Declared by | Example |
| --- | --- | --- |
| The app | `prefix` in `hinoki.toml` | `/billing` |
| A version inside the app | `Mount` in `routes.py` | `/v2` |
| The resource | `RouteGroup(...)` or `API, path=...` | `/invoices` |

Write each segment once. Paths in code are relative to the app, so moving an app is a one-line change in `hinoki.toml`.

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

Keep `app` at module level in the module that `project.application` names. Use a factory only when building the application needs values that exist at run time, or when you build it several times:

```python
# notes/main.py
from hinoki import Hinoki

from notes.settings import ProjectSettings


def create_app() -> Hinoki:
    return Hinoki(settings=ProjectSettings, title="Notes")
```

Name it in the manifest as `application = "notes.main:create_app"`. A `Hinoki(...)` inside a function of that module binds the manifest routes too.

## Explicit routes: tests, scripts and options

Only the module named by `project.application` binds the manifest. Anywhere else, `Hinoki()` starts empty, and `include=` says what it serves:

```python
# tests/test_billing_routes.py
from hinoki import Hinoki, Mount

from notes.apps.billing.routes import routes

app = Hinoki(include=[Mount("/billing", routes)])
```

An explicit `include=` wins over the manifest, also in the application module, and `include=[]` is an empty application. To add routes that exist only when a setting is on, call `app.include(...)` in the application module before it serves:

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
