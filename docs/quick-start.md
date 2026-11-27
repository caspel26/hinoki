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
│   └── apps/
│       └── entries/   starter app registered in hinoki.toml
│           ├── api.py
│           ├── routes.py
│           ├── models.py
│           └── migrations/
├── tests/             pytest suite
└── AGENTS.md          agent instructions
```

The generated project serves `/health`. The `entries` app starts with an empty
route list and space for its models and migrations. Choosing no at **Create an
app now?** creates just the project. [Add another app](#add-another-app) shows
how to add one later.

!!! tip "Scripts and CI"
    `uv run hinoki new notes --dir .. --no-input --database sqlite --git --install --app entries`
    gives the same answers without prompts. Plain `--no-input` uses the defaults
    instead: no database, no `git init`, no `uv sync` and no app. All options are
    in [`hinoki new`](new-project.md).

## Check and test it

```sh
cd ../notes
uv run hinoki check
uv run pytest
```

`hinoki check` runs the project's rules (the eight listed in `notes/rules.py`)
and prints one line for each; a violation names the file, the line and a fix.
The test calls `/health` in process and needs no database. Run both again after
every change, as `AGENTS.md` tells an agent to.

## Serve it

```sh
uv run hinoki dev
```

`hinoki dev` serves on `http://127.0.0.1:8000` and reloads when a file changes. In another terminal:

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
`uv run hinoki routes` to inspect the route map that `Hinoki(settings=ProjectSettings, ...)`
loaded automatically from `notes/routes.py` (`[project] routes` in `hinoki.toml`):

```sh
uv run hinoki routes
```

```text
GET     /health  public                   notes.api.health
```

## What the manifest does

`hinoki.toml` registers the application, the project's route map (`[project] routes`),
the rules it runs and each app's persistence and dependencies. `notes/main.py`
loads `[project] routes` automatically, while `notes/routes.py` decides which
apps are mounted over HTTP:

```toml
[project]
application = "notes.main:app"
routes = "notes.routes:routes"
rules = ["notes.rules:project_rules"]
```

`notes/main.py` declares the application:

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

`Hinoki(settings=ProjectSettings, ...)` reads `hinoki.toml` automatically,
loading `notes/routes.py` (`[project] routes`) and `rules`, so `main.py` stays
short. Commands read the same manifest, so inside the project they need no
module paths.

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
├── api.py         starter for the app’s endpoints
├── routes.py      empty app route list
├── models.py      starter for the app’s models
└── migrations/    migration package
```

It is registered in `hinoki.toml`:

```toml
[[apps]]
name = "entries"
models = "notes.apps.entries.models"
migrations = "notes.apps.entries.migrations"
requires = []
```

After adding an endpoint or a group to the app's own route list, mount that list
in the project's route map:

```python
# notes/routes.py
from hinoki import Includable, Mount

from notes.api import health
from notes.apps.entries.routes import routes as entries_routes

routes: list[Includable] = [health, Mount("/entries", entries_routes)]
```

Endpoints inside the app declare paths relative to the `/entries` prefix.

App names are lower-case letters and digits with single underscores between words
(for example `api_v1`), starting with a letter. Define the
app's models in `notes/apps/entries/models.py`, then
`uv run hinoki migrations plan entries`; [9. Projects and apps](tutorial/8-projects.md)
walks through it.

### Add another app

If you answered no, or want a second app, create it from the project:

```sh
uv run hinoki app create billing
```

## Next

- [Tutorial](tutorial/index.md): build an API layer by layer.
- [9. Projects and apps](tutorial/8-projects.md): add an app with its own
  models and migrations to this project.
- [Notes example](https://github.com/caspel26/hinoki/tree/main/examples/notes):
  a separate, complete project with a model, schemas and generated CRUD routes.
