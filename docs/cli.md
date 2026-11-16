---
status: implemented
---

# Command line

The `hinoki` command accepts a `module:attribute` target for an application, factory, settings class or model module as documented by each subcommand. Targets are imported from the current project directory.

Targets such as `notes.main:app`, `tracker.app:create_app` and
`blog.main:create_app` refer to the checked-in project in `examples/`. Run
those commands from `examples/`; when using the repository checkout, prefix
them with `uv run --project ..` to use the locked environment at the parent.

## Commands and options

| Command | Purpose |
| --- | --- |
| `hinoki new NAME` | Create a starter project with settings, rules, tests and agent instructions. |
| `hinoki app list` | List registered apps, their model and migration modules, and dependencies without importing app code or opening a database. `--format json` is machine readable. |
| `hinoki app create NAME` | Add a registered modular app with its own models and migration package. |
| `hinoki doctor [TARGET]` | Summarize environment, manifest and app construction diagnostics without serving or connecting to a database. |
| `hinoki dev TARGET` | Run an app with reload during development. |
| `hinoki serve TARGET` | Run without reload, using the `server` settings group. |
| `hinoki check TARGET` | Run the application's configured rules; `--rules` and `--package` select rule and source targets. |
| `hinoki routes TARGET` | List every method, path, handler and access policy in path order. `--path` and `--method` filter; `--format json` is machine readable. Nothing is served. |
| `hinoki openapi TARGET` | Print or write the generated OpenAPI document, or check a JSON file for drift. Nothing is served and no database is opened. |
| `hinoki version` | Show the Hinoki version; `--verbose` includes Python, platform, native core and optional Granian details. `--format json` is machine readable. |
| `hinoki settings TARGET` | Show effective settings and their source, with secrets redacted. `--dotenv` and `--toml` select optional files. |
| `hinoki migrations ...` | Plan, inspect and apply project app migrations in dependency order. Legacy explicit targets remain available without a manifest. |
| `hinoki manage [TARGET] [COMMAND]` | Run an app-defined command with its declared module resources; a project manifest supplies the default target. |
| `hinoki shell [TARGET]` | Open an interactive Python shell with the app's modules and registered models. |
| `hinoki mcp TARGET` | Serve project context to an MCP client over stdio. Scenario tools are off by default. |

### `doctor`

```sh
uv run hinoki doctor
uv run hinoki doctor notes.main:app --format json
```

With no target, `doctor` uses `project.application` from `hinoki.toml`. If there
is no manifest or default target, it reports environment checks and marks app
checks as skipped. An explicit `TARGET` can be an app or factory. Loading it
imports your Python code, so module-level code and the factory may have their
own side effects. Hinoki does not start app or module lifespans, run hooks,
start a server, run architecture rules, connect to a database, or write files.

The report covers manifest validity, Python/native extension and optional
Granian availability, route registration, configured typed settings, and
in-memory OpenAPI generation. An app with no settings object reports that
check as skipped; a custom non-`Settings` object is a warning. JSON output has
`schema_version: 1` and stable check IDs. Settings values and exception
messages are not included. Exit status is `0` when no
diagnostic failed, `1` for invalid manifest or app diagnostics, and `2` when a
requested target cannot be loaded. Warnings and skipped checks remain visible.

Database connectivity and applied migration state are intentionally separate:
use [`hinoki migrations status`](migrations.md) for the live ledger and
[`hinoki migrations plan --check`](migrations.md#check-migration-drift) for
model-to-file drift.

### `new`

```sh
hinoki new notes --dir .
```

`NAME` is a lowercase package name containing letters and digits; it cannot be `hinoki` or a Python standard-library module name, because those names would shadow imports used by the generated project. `--dir` chooses the parent directory and defaults to the current directory. The target `PATH/NAME` must not exist or must be empty; nothing is overwritten, and nothing is installed or run.

This repository's pre-alpha Hinoki framework is not published on PyPI; the [`hinoki` name](https://pypi.org/project/hinoki/) belongs to an unrelated legacy package. The generated `pyproject.toml` therefore names this repository as the source under `[tool.uv.sources]`:

| Given | Source written |
| --- | --- |
| `--hinoki /path/to/checkout` | that checkout, editable |
| `--hinoki https://github.com/caspel26/hinoki@REV` (or `git+https://...`) | that repository, pinned to `REV` when given |
| nothing, running from a source checkout | the checkout you ran it from |
| nothing, any other installation | the public repository, default branch |

The result is `cd PATH/NAME && uv sync && uv run pytest`: the project has a health route, typed settings, rules, one test that needs no database, a `README.md`, `AGENTS.md` and `.mcp.json`.
Checkout paths and Git source values are escaped as TOML strings in the generated `pyproject.toml`.

### `app`

The generated `hinoki.toml` registers the default app and its models and migrations. Add modules without moving files by creating another app:

```sh
uv run hinoki app create billing
```

This creates `NAME/apps/billing/{models.py,migrations/}` and registers both dotted module paths. Cross-app foreign keys require the referencing app to list the referenced app in `requires` in `hinoki.toml`; migrations are then planned/applied in dependency order. The project registry is explicit: Hinoki does not scan arbitrary installed packages or run imports at install time.

List the registered apps from the project root or any subdirectory:

```sh
uv run hinoki app list
uv run hinoki app list --format json
```

The text output shows each app in dependency order with its model module, migration package and prerequisites. JSON contains the same fields under an `apps` key. The command only reads and validates `hinoki.toml`: it does not import app modules, connect to a database or start the app. Without a manifest, it prints an error and exits 2.

### `dev` and `serve`

```sh
hinoki dev notes.main:app --host 127.0.0.1 --port 8000
hinoki serve notes.main:app --workers 4
hinoki serve notes.main:app --server rust --workers 2
```

Both commands accept `--host` and `--port`; they override `settings.server.host` and `.port`. `serve` also accepts `--workers`, overriding `.workers`. Both accept `--server granian|rust`, overriding `.server` (default `granian`). Otherwise values come from the app's `settings.AppSettings.server` group. `dev` enables reload and runs one worker. Granian is provided by the optional server extra.

`--server rust` runs the application on the Rust HTTP server (see [Running an application](serving.md#the-rust-server) for what it does and does not do). A setting that the chosen server cannot honour (`access_log` with `rust`; `max_connections`, `header_read_timeout` or `body_read_timeout` with `granian`) makes the command print `error: server.<name> is not supported by the <server> server` and exit with status 2. A build whose native extension has no Rust server exits 2 with a message instead of falling back to Granian. With several workers the command runs a supervisor that starts one process per worker; `SIGTERM` or Ctrl-C stops them gracefully, and a worker that dies stops the rest and makes the command exit non-zero.

### `check`

```sh
hinoki check tracker.app:create_app \
  --package tracker --phase startup --format text
```

Options:

* `--rules MODULE:ATTRIBUTE` selects a `RuleSet`; the app's rules are used by default.
* `--package PACKAGE` selects a package for static import checks. Repeat it for multiple packages. Without it, the package containing the target is scanned.
* `--phase startup|static` selects a phase; the default runs both.
* `--waivers PATH` selects a waiver file; the default is `hinoki-rules.toml` in the current directory.
* `--format text|json` selects human-readable or versioned JSON output.
* `--allow-effects` also runs checks declared as effectful, which may access files, network or databases.

Exit status is `0` when no rules fail, `1` for violations, and `2` when the target or checks cannot run.

### `openapi`

```sh
hinoki openapi notes.main:app
hinoki openapi notes.main:app --output openapi.json
hinoki openapi notes.main:app --check openapi.json
```

The command builds the app without serving it or opening a database; it works even when `openapi_url` is disabled. With no file option it prints deterministic JSON (sorted keys, two-space indentation and a trailing newline). `--output FILE` writes those bytes. `--check FILE` compares parsed JSON, so formatting differences do not count as drift. Exit status is `0` when the file matches or is written, `1` when its JSON differs, and `2` when the target cannot load or the check file is missing or invalid JSON. `--output` and `--check` cannot be used together.

### `version`

```sh
hinoki version
hinoki version --verbose
hinoki version --verbose --format json
```

The default prints `hinoki X.Y.Z`. `--verbose` adds the Python version, platform, native extension path and Granian version; Granian is reported as `not installed` when its optional package is absent. Use `--format json` for machine-readable output. Details the binding does not expose, including its bundled SQLite library version, are omitted rather than inferred.

### `settings`

```sh
hinoki settings blog.settings:BlogSettings \
  --prefix BLOG_ --dotenv .env --toml settings.toml --format json
hinoki settings blog.settings:BlogSettings --example > .env.example
```

`TARGET` can be a settings class or an app with typed settings. `--prefix`, `--dotenv` and `--toml` override sources declared by the settings class. `--format text|json` selects output; `--example` prints a `.env.example` with defaults and types, leaving secrets and required fields blank. Values of `settings.Secret` are redacted.

### `manage`

```sh
hinoki manage --help
hinoki manage greet Peppe
```

Commands are declared on the app with `Hinoki(commands=[...])`. Each command subclasses `hinoki.management.Command`, gives itself a lower-case name, and adds arguments through `add_arguments(parser)`. In a scaffolded project, `hinoki manage` lists commands and `hinoki manage greet Peppe` runs one; the manifest's `project.application` supplies the app target. Outside a registered project, use `hinoki manage TARGET COMMAND`. `--help` after a command shows its arguments.

```python
from argparse import ArgumentParser, Namespace

from hinoki import Hinoki
from hinoki.management import Command, CommandContext


class Greet(Command):
    name = "greet"
    help = "print a greeting"

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("name", nargs="?", default="world")

    def handle(self, _context: CommandContext, args: Namespace) -> None:
        print(f"Hello, {args.name}!")


app = Hinoki(commands=[Greet])
```

The same example is in `examples/project/app.py`; the examples manifest supplies
its target when you run the command from the `examples/` project directory.

`handle(context, args)` may be synchronous or asynchronous. A sync handler runs on Hinoki's bounded executor. It can read `context.settings` and retrieve resources with `context.get(Type)` or `context.find(Type)`. Declare module classes in the command's `requires` tuple; Hinoki starts those modules and their dependencies for the command, then closes them before it exits. Other app modules are not started. A command with no requirements starts no app modules.

Commands run inside the project process and can modify data. Use disposable databases when developing or testing a command that writes. A handler returns `None` for success or an exit code from 0 through 255. Unknown commands and invalid arguments exit 2; a command failure exits nonzero. App settings load when Hinoki constructs the app, but module resources open only for the selected command.

### `shell`

```sh
uv run hinoki shell
uv run hinoki shell blog.main:app --models blog.models
```

In a project with `hinoki.toml`, the target defaults to `project.application`
and the shell loads every registered app's model module. Repeat `--models`
to select model modules explicitly. Outside a project, pass a target; `--models`
is optional. The interactive namespace contains `app`, its validated
`settings`, and concrete model classes. Conflicting model class names fail
before the prompt.

The shell keeps one event loop and supports top-level `await`:

```pycon
>>> await Note.all()
[Note(id=1, title='Hello')]
>>> app.title
'Hinoki Blog'
```

Use async ORM queries with `await`; synchronous ORM methods cannot block the
shell's event loop. Unmanaged tasks are cancelled on exit and cleanup is
bounded by `app.runtime.shutdown_timeout`.

Installed module resources are started and closed for the session, so ORM
queries use the app's configured database. App `on_startup` and `on_shutdown`
hooks do not run, and migrations are never applied. The shell requires a TTY;
Ctrl-D exits, Ctrl-C at the prompt exits, and Ctrl-C during a cell interrupts
that cell. It executes Python with the current user's process privileges and
may write to the configured database, so use a development database when
experimenting.

### `migrations`

In a project created by `hinoki new`, migration modules are read from the manifest; users do not supply model or migration paths:

```sh
uv run hinoki migrations plan --name add-index --dry-run
uv run hinoki migrations plan --check
uv run hinoki migrations plan --app billing --name add-invoice-index
uv run hinoki migrations status
uv run hinoki migrations apply
uv run hinoki migrations sql --app billing 0001-initial
```

`status` and `apply` cover all registered apps in dependency order. Writing a
`plan` in a multi-app project requires `--app`; read-only `plan --check` and
`plan --dry-run` without `--app` cover every app in dependency order. `sql`
likewise requires an app in a multi-app project. `--name` is optional for a
focused plan: Hinoki derives a stable slug from the first planned schema change
(for example, `create-table-notes`); pass `--name initial-notes` to override
it. All-app previews cannot use `--name`. Numbering is automatic. `apply`
initializes the configured local SQLite database when no database URL is set.
In a manifest project the implicit `.hinoki/local.sqlite3` file is rooted
beside `hinoki.toml`, even if you run the command from a subdirectory; explicit
relative SQLite URLs remain relative to the current working directory. Added
apps have separate migration ledger namespaces, so identical numbered
filenames do not collide; the scaffolded root app retains its existing
unprefixed migration keys. `--app billing` includes its declared prerequisite
apps for migration application.

Use `plan --check` in CI to fail when model changes are missing from committed
migration files. It exits `1` when operations are pending and `0` when the
files match the models. It is read-only: it imports registered model modules
and the selected migration package but does not write migration files or
connect to a database. Project planning loads every registered app's models
to validate cross-app foreign keys, even when `--app` selects one migration
package. Without `--app`, it checks every app in dependency order; `--app NAME`
limits the check to that app and its prerequisites. Destructive operations
are reported as pending too. This checks model-to-file drift only;
`migrations status` checks which migration files have been applied to a live
database. `plan --dry-run` without `--app` previews every app and exits
successfully when it prints planned operations. The all-app preview is
hypothetical and writes no files; a dependent plan shown there may need its
prerequisite migration generated and reviewed first. Ordinary app-local
destructive changes are previewed without requiring `--allow-destructive`.

Before writing a focused plan, Hinoki verifies that cross-app foreign-key
targets exist in the prerequisite app's recorded migration state and have a
compatible primary-key column. It also refuses parent teardowns while any
recorded state of another app still refers to the target, including for
`--dry-run` and `--check` and even with `--allow-destructive`. An all-app
preview fails without partial diffs in this case. Remove and apply the child
foreign key in a separate deployment phase before preparing the parent teardown. See
[`migrations`](migrations.md#cross-app-migration-safety) for the limits of this
file-based check.

Without a `hinoki.toml`, the explicit legacy form remains:

```sh
hinoki migrations --dir myapp/migrations plan \
  --models myapp.models --name add-index --dry-run
hinoki migrations --dir myapp/migrations plan \
  --models myapp.models --name add-index --allow-destructive
export HINOKI_DATABASE_URL="$APP_DATABASE__URL"
hinoki migrations --dir myapp/migrations status
hinoki migrations --dir myapp/migrations apply
hinoki migrations --dir myapp/migrations sql 0001-initial
```

`plan` requires `--models MODULE`; `--name` is an optional slug override and is derived from the first planned schema change when omitted. `--dry-run` prints operations without writing files. Destructive operations require `--allow-destructive`. Without `HINOKI_DATABASE_URL`, plan/status/apply target the local SQLite file at `.hinoki/local.sqlite3`; `--database URL` selects another supported backend. A URL given as an argument is visible in the process list and shell history, so keep a URL containing a password in the environment. When the connection fails, the command prints `error: database: …` with a `fix:` line and exits 1; the URL is not printed. `sql NAME` prints a migration's SQL. Use a disposable database while reviewing migrations.

### `mcp`

```sh
hinoki mcp tracker.app:create_app \
  --rules tracker.rules:tracker_rules \
  --package tracker --models tracker.models
```

The development server uses MCP over stdio. It accepts `--rules`, repeatable `--package`, repeatable `--models`, and `--waivers` with the same target meanings as `check`. `--scenarios off|read|write` controls whether `run_scenario` is registered: `read` allows GET, HEAD, OPTIONS and QUERY; `write` allows every supported method. Scenarios start the real app, run handlers and lifespan hooks, and may write data even for a GET. Keep the tool server owner-controlled and point the app at a disposable database.

The development MCP is separate from application MCP, which is mounted with `expose_as_mcp` and an explicit action allowlist. See the [development MCP guide](dev-mcp.md) for tool inputs and results.

## Import target examples

```sh
hinoki dev notes.main:app
hinoki check tracker.app:create_app
hinoki settings blog.settings:BlogSettings
hinoki migrations plan --app blog --name add-index --dry-run
```

When a target import fails, check that the command runs from the project directory, the package has `__init__.py` files where needed, and the attribute name is correct.
