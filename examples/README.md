# Hinoki examples

Three applications that share one project. [`hinoki.toml`](hinoki.toml)
registers each one as an app with its own models and migrations, plus the
admin's user tables. Each example is a project package (`main.py`, settings,
rules) with its app under `apps/<name>/`: the `hinoki new` then
`hinoki app create` layout, with the models inside the app and not in the
project package.

| Example | Shows | Application |
| --- | --- | --- |
| [Notes](notes/README.md) | the smallest app: a model, schemas, a route group | `notes.main:app` |
| [Tracker](tracker/README.md) | layers, bearer access, rules, an MCP tool | `tracker.main:app` |
| [Blog](blog/README.md) | generated CRUD, relations, login, the admin | `blog.main:app` |
| `project/` | a management command | `project.app:app` (the manifest's application) |

Each application is declared once, as `app = Hinoki(...)` in its `main.py`.

## Run

From this directory; `uv` finds the repository's environment one level up.

```sh
uv run hinoki app list
uv run hinoki migrations apply
uv run hinoki dev notes.main:app
```

`migrations apply` creates `.hinoki/local.sqlite3` here and applies every app in
dependency order; `uv run hinoki migrations apply notes` limits it to one app and
its prerequisites. Migration commands use the manifest application's database:
set `EXAMPLES_DATABASE__URL` (or pass `--database`) to apply them elsewhere.
`uv run hinoki manage greet Ada` runs the management command, and
`uv run hinoki shell` opens a shell with the registered models.

> [!NOTE]
> Server and check commands name their application here because this
> directory hosts several. In a project created with `hinoki new`,
> `hinoki.toml` names the application and the commands take no target.
