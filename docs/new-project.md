---
status: implemented
related:
  - getting-started.md
  - cli.md
  - serving.md
---

# Create a project

`hinoki new` creates a working project: an application with a health route,
typed settings, architecture rules, a test and instructions for AI agents.
From the Hinoki checkout:

```sh
uv run hinoki new
```

## Answer the questions

In a terminal, `hinoki new` asks for everything the command line leaves out:

| Question | Answers | Default |
| --- | --- | --- |
| Project name | lower-case letters and digits, single underscores between words, starting with a letter | none |
| Parent directory | any path | `.` (here) |
| Database | `none`; `sqlite` (local file in `.hinoki/`); `postgresql` (also writes `.env.example`) | `none` |
| Server | `kiso` (Hinoki's own Rust server: native handler lane, access log); `granian` (general-purpose ASGI server: runs any ASGI app) | `kiso` |
| Agent instructions and MCP config? | yes / no | yes |
| Agent skills? | yes / no | no |
| Initialize a git repository? | yes / no | yes |
| Install dependencies (uv sync)? | yes / no | yes |
| Create an app now? | yes / no | yes |
| App name (only after yes) | the same rule as the project name; an invalid name is asked again | `core` |

Each answer collapses into a summary line. When the files are written, the
command lists the key ones, creates the app you named with the same code as
[`hinoki app create`](cli.md#app) (and registers it in `hinoki.toml`), runs
`git init` and `uv sync` with a timer, and ends with the next commands to type.
When no app was created, the summary says how to add one. Nothing is written
until the last answer, so Ctrl-C, Esc or end of input cancels cleanly (exit 130).

## Create a project without questions

Every question has a flag. `--no-input` never asks and uses the defaults for
what is missing, which suits scripts and CI (without a terminal no question is
asked either):

```sh
uv run hinoki new notes --no-input --database sqlite --git --install
```

| Option | Effect |
| --- | --- |
| `NAME` | the package name; required with `--no-input` |
| `--dir PATH` | parent directory; the project is `PATH/NAME` |
| `--database none\|sqlite\|postgresql` | see [Choose a database](#choose-a-database) |
| `--server kiso\|granian` | see [Choose a server](#choose-a-server) |
| `--agent-files` / `--no-agent-files` | write `AGENTS.md`, `CLAUDE.md` and `.mcp.json` |
| `--skills` / `--no-skills` | also write the [agent skills](cli.md#skills) under `.claude/skills` |
| `--git` / `--no-git` | run `git init`; no commit is made. Without a prompt: off |
| `--install` / `--no-install` | run `uv sync`. Without a prompt: off |
| `--app NAME` | also create this app and register it, without asking. The name is checked before anything is written |
| `--no-app` | create no app and do not ask about one; the two flags exclude each other |
| `--hinoki SOURCE` | where the project installs Hinoki from; see [below](#where-hinoki-comes-from) |

## What you get

```text
notes/
├── hinoki.toml          project registry: application, routes, apps
├── hinoki-rules.toml    rule waivers (an example, commented out)
├── pyproject.toml       dependencies, pytest, Ruff
├── README.md
├── AGENTS.md            agent instructions (with CLAUDE.md and .mcp.json)
├── notes/
│   ├── main.py          the application
│   ├── api.py           the health endpoint
│   ├── routes.py        the routes hinoki.toml points to
│   ├── settings.py      typed settings (NOTES_ variables and .env)
│   └── rules.py         architecture rules
└── tests/
    └── healthtest.py    one test, no database needed
```

`hinoki.toml` names the application, so project commands find it on their own.
`notes/main.py` creates `Hinoki(settings=ProjectSettings, ...)` and automatically
loads `[project] routes` (`notes/routes.py`), where you mount exposed apps with
`Mount("/<name>", ...)`. The `rules` list in the same table is how the
application gets `notes/rules.py` (see [which rules run](rules.md#which-rules-run)).

```toml
# hinoki.toml
[project]
application = "notes.main:app"
routes = "notes.routes:routes"
rules = ["notes.rules:project_rules"]
```

The project package holds no models and registers no app. Models live in apps:
The last question of `hinoki new` (or `--app billing`) creates the first one;
`uv run hinoki app create billing` adds more later. Either way it writes
`notes/apps/billing/` and adds the `[[apps]]` entry for it
([`hinoki app create`](cli.md#app)).

## Run it

```sh
cd notes
uv run pytest
uv run hinoki dev
```

Run `uv sync` first if you answered no to installing. The application answers
on <http://127.0.0.1:8000/health>, with its documentation at `/docs`.

## Choose a database

`sqlite` and `postgresql` add one field to `notes/settings.py`:

```python
class ProjectSettings(settings.AppSettings, prefix="NOTES_", dotenv=".env"):
    database: settings.DatabaseSettings
```

Its value comes from `NOTES_DATABASE__<FIELD>` variables (two underscores
between the group and the field) in the environment or in `.env`; keep `.env`
out of git. With no value set it is the SQLite file
`.hinoki/local.sqlite3`. `postgresql` also writes `.env.example` with a
placeholder URL; copy it to `.env` and fill it in:

```sh
# .env.example
NOTES_DATABASE__URL=postgresql://user:password@localhost:5432/notes
```

`none` declares no field, and a request that queries a model fails with
`no database is active`. Add the field above when you add models. [Configure a
database](databases.md) lists the URL forms and the connection fields.

No database is created or contacted. Create the tables with
[`hinoki migrations apply`](migrations.md) once you have models.

## Choose a server

`kiso` needs no configuration. `granian` writes `settings.toml` with
`[server] server = "granian"`, which the settings class reads. Either way,
`NOTES_SERVER__SERVER` and `--server` on `dev` and `serve` override the choice.
[Serve an application](serving.md) compares the two.

## Where Hinoki comes from {#where-hinoki-comes-from}

This repository's pre-alpha Hinoki framework (package `hinoki-api`) is not published on PyPI yet; the [`hinoki` name](https://pypi.org/project/hinoki/) belongs to an unrelated legacy package. The generated `pyproject.toml` therefore names this repository as the source under `[tool.uv.sources]`:

| Given | Source written |
| --- | --- |
| `--hinoki /path/to/checkout` | that checkout, editable |
| `--hinoki https://github.com/caspel26/hinoki@REV` (or `git+https://...`) | that repository, pinned to `REV` when given |
| nothing, running from a source checkout | the checkout you ran it from |
| nothing, any other installation | the public repository, default branch |

Checkout paths and Git source values are escaped as TOML strings.

## How it looks in a terminal

On a colour, UTF-8 terminal (macOS and Linux) the prompts sit on a guide rail and answer to keys: type a name (backspace edits, Ctrl-U clears), move through the database and server lists with ↑/↓ or pick one by number, and switch yes/no with ←/→ or `y`/`n`; Enter confirms. Each answered prompt collapses into one summary line. The run then lists the key files it created, times `git init` and `uv sync` (with a spinner while they run) and ends with the next commands. Every colour terminal shows the same banner: a branch (`╭─`, `│`, `╰─`) titled `Hinoki` with the tagline and the version, then the Python version and the documentation and source addresses; `hinoki app create` shows the same banner. Elsewhere the prompts are numbered lists and `[Y/n]` questions answered with Enter.

Output that is piped, `NO_COLOR` and `TERM=dumb` give plain ASCII with no escape sequences. `HINOKI_ANIMATE=0` (or `CI`) turns off the tagline animation and the spinner. When the terminal reports a light background through `COLORFGBG`, darker shades of the same colours are used. Ctrl-C or Esc cancels before anything is written (exit status 130); so does the end of input.

## Errors and edge cases

- The name must be lower-case letters and digits, with single underscores between
  words (`my_shop`, `api_v1`), starting with a letter, and not
  `hinoki`, a Python keyword or a standard-library module such as `json`, which
  would shadow
  imports in the project. A refused name is asked again; with `--no-input` the
  command exits 2.
- An app name follows the same rule. A refused name is asked again; `--app` with a bad
  name exits 2 before anything is written. If the project is written but the
  app cannot be created, the error is printed, the project stays and the exit
  status is 2.
- `PATH/NAME` must not exist or must be empty; nothing is overwritten.
- `git` or `uv` missing or failing prints a warning and keeps the project.
- Nothing else is installed or run.

## See also

- [Installation](getting-started.md)
- [Quick start](quick-start.md)
- [Command line](cli.md)
- [Test an application](testing.md)
