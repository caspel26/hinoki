---
status: implemented
related:
  - tutorial/8-projects.md
  - cli.md
  - serving.md
---

# Quick start

Create a project, run its tests and serve it. Everything runs locally; the
database is a SQLite file.

## Create a project

From the Hinoki checkout:

```sh
uv run hinoki new
```

`hinoki new` asks only what it needs. Give these answers and keep the other
defaults:

| Prompt | Default | Answer |
| --- | --- | --- |
| Project name | | `notes` |
| Parent directory | `.` | `..` (beside the checkout) |
| Database | `none` | `sqlite` |
| Server | `kiso` | `kiso` |
| Agent instructions and MCP config? | yes | yes |
| Agent skills? | no | no |
| Initialize a git repository? | yes | yes |
| Install dependencies (uv sync)? | yes | yes |
| Create an app now? | yes | yes |
| App name | `core` | `entries` |

It writes the project and the `entries` app, runs `git init` and `uv sync`, and
prints the next commands. The files that matter:

```text
notes/
├── hinoki.toml        manifest: application, routes and the entries app
├── notes/
│   ├── main.py        the application
│   ├── api.py         GET /health
│   ├── routes.py      routes = [health]
│   ├── settings.py    typed settings, NOTES_* variables
│   ├── rules.py       architecture rules
│   └── apps/entries/  models, endpoints, routes and migrations of the app
├── tests/             pytest suite
└── AGENTS.md          agent instructions
```

The project package holds no models. They live in apps; answering no to the
last question leaves the project without one, and
[Add another app](#add-another-app) shows how to add it later.

!!! tip "Scripts and CI"
    `uv run hinoki new notes --dir .. --no-input --database sqlite --git --install --app entries`
    gives the same answers without prompts. Plain `--no-input` uses the defaults
    instead: no database, no `git init`, no `uv sync` and no app. All options are
    in [`hinoki new`](new-project.md).

## Test and serve it

```sh
cd ../notes
uv run pytest
uv run hinoki dev
```

The test calls `/health` in process and needs no database. `hinoki dev` serves
on `http://127.0.0.1:8000` and reloads when a file changes. In another terminal:

```sh
curl http://127.0.0.1:8000/health
```

```text
{"status":"ok"}
```

At startup `hinoki dev` logs a warning that `allowed_hosts is '*'`: the
application answers to any `Host` header. It is harmless on your machine. To
name the hosts you serve, and silence it, add
`NOTES_ALLOWED_HOSTS=127.0.0.1,localhost` to `.env`; see
[`allowed_hosts`](web-security.md#allowed_hosts).

Open `http://127.0.0.1:8000/docs` for the generated OpenAPI UI, or run
`uv run hinoki routes` to list every route with its access policy.

## What the manifest does

`hinoki.toml` names the application and where its routes live; each app you
create adds its own models, migrations and routes:

```toml
[project]
application = "notes.main:app"
routes = "notes.routes:routes"
```

`notes/main.py` declares the application:

```python
# notes/main.py
from hinoki import Hinoki
from hinoki.project import manifest_routes

from notes.rules import project_rules
from notes.settings import ProjectSettings

app = Hinoki(
    settings=ProjectSettings,  # NOTES_* variables and .env
    include=manifest_routes(),  # notes/routes.py, then each app's routes at its prefix
    title="Notes",
    openapi_url="/openapi.json",
    docs_url="/docs",
    rules=project_rules,
)
```

`manifest_routes()` reads `hinoki.toml`, so the application lists no routes of
its own. Commands read the same file, so inside the project they need no module
paths.

## Where the database is configured

Answering `sqlite` added one field to `notes/settings.py`:

```python
# notes/settings.py
from hinoki import settings


class ProjectSettings(settings.AppSettings, prefix="NOTES_", dotenv=".env"):
    database: settings.DatabaseSettings
```

With no value set, it is the SQLite file `.hinoki/local.sqlite3`. Each field
of `DatabaseSettings` is a `NOTES_DATABASE__<FIELD>` variable (two underscores
between the group and the field), read from the environment or from `.env` in
the project directory. `uv run hinoki settings` lists every variable and where
its value came from. To use another file:

```sh
# .env
NOTES_DATABASE__URL=sqlite://./.hinoki/notes.sqlite3
```

[Configure a database](databases.md) covers PostgreSQL, MySQL and MariaDB, and
connection fields instead of a URL. The project has no models yet; add them in
the app, as [the next section](#your-first-app) and the
[tutorial](tutorial/index.md) show.

## Your first app

Models live in apps, not in the project package. `hinoki new` created `entries`
when you answered the last question:

```text
notes/apps/entries/
├── api.py         endpoints of the app
├── routes.py      the routes served, mounted at /entries
├── models.py      tables of the app
└── migrations/    migration history
```

It is registered in `hinoki.toml`:

```toml
[[apps]]
name = "entries"
models = "notes.apps.entries.models"
migrations = "notes.apps.entries.migrations"
routes = "notes.apps.entries.routes:routes"
prefix = "/entries"
requires = []
```

App names are lower-case letters and digits, with no underscores. Define the
app's models in `notes/apps/entries/models.py`, then
`uv run hinoki migrations plan entries`; [8. Projects and apps](tutorial/8-projects.md)
walks through it.

### Add another app

If you answered no, or want a second app, create it from the project:

```sh
uv run hinoki app create billing
```

## Next

- [Tutorial](tutorial/index.md): build an API layer by layer.
- [8. Projects and apps](tutorial/8-projects.md): add an app with its own
  models and migrations to this project.
- [Notes example](https://github.com/caspel26/hinoki/tree/main/examples/notes):
  a small CRUD API whose model lives in `notes/apps/notes`.
