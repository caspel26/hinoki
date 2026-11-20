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

It writes the project, runs `git init` and `uv sync`, and prints the next
commands. The files that matter:

```text
notes/
├── hinoki.toml        manifest: application, routes, apps
├── notes/
│   ├── main.py        the application
│   ├── api.py         GET /health
│   ├── routes.py      routes = [health]
│   ├── settings.py    typed settings, NOTES_* variables
│   └── rules.py       architecture rules
├── tests/             pytest suite
└── AGENTS.md          agent instructions
```

!!! tip "Scripts and CI"
    `uv run hinoki new notes --no-input` skips the prompts and uses the
    defaults. All options are in [`hinoki new`](new-project.md).

## Test and serve it

<!-- post-G1: today `uv run hinoki dev notes.main:app` -->
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

<!-- post-G1: today `uv run hinoki routes notes.main:app` -->
Open `http://127.0.0.1:8000/docs` for the generated OpenAPI UI, or run
`uv run hinoki routes` to list every route with its access policy.

## What the manifest does

`hinoki.toml` names the application and where its routes, models and migrations
live:

```toml
[project]
application = "notes.main:app"
routes = "notes.routes:routes"

[[apps]]
name = "notes"
models = "notes.models"
migrations = "notes.migrations"
requires = []
```

The application in `notes/main.py` lists no routes: it binds the ones the
manifest names. Commands read the same file, so inside the project they need
no module paths.

## Next

- [Tutorial](tutorial/index.md): build an API layer by layer.
- [8. Projects and apps](tutorial/8-projects.md): add an app with its own
  models and migrations to this project.
- [Notes example](https://github.com/caspel26/hinoki/tree/main/examples/notes):
  a small CRUD API with a migration.
