---
status: implemented
related:
  - new-project.md
  - inspect.md
  - serving.md
---

# Command line

Every task in a Hinoki project has a `hinoki` command. Inside a project created
by `hinoki new`, run them through `uv run`, which uses the project's locked
environment:

```sh
uv run hinoki dev
uv run hinoki check
uv run hinoki migrations apply
```

## Commands

| Group | Command | What it does | Guide |
| --- | --- | --- | --- |
| Start | `hinoki new [NAME]` | Create a project; asks for what the flags leave out, ending with "Create an app now?" (`--app NAME`, `--no-app`). | [Create a project](new-project.md) |
| | `hinoki app create\|add\|list` | Add an app, register an existing package, list the apps. | [below](#app) |
| | `hinoki dev` / `serve` | Run with reload / run for production, on Kiso or Granian; `--interface wsgi` serves a WSGI callable. | [Serve](serving.md) |
| | `hinoki shell` | Python shell with the app, its settings and models, top-level `await`. | [Shell](shell.md) |
| Inspect | `hinoki check` | Run the architecture rules. | [Inspect](inspect.md#check) |
| | `hinoki doctor` | Environment, manifest and app diagnostics. | [Inspect](inspect.md#doctor) |
| | `hinoki routes` | Every route with its access policy and server lane. | [Inspect](inspect.md#routes) |
| | `hinoki events` | Every event handler in execution order, the event limits and the apps' `events` targets. | [Inspect](inspect.md#events) |
| | `hinoki mail check` | Read and check every registered email template against its context. | [Email templates](email-templates.md#checked-at-startup) |
| | `hinoki mail preview NAME` | Render one registered email template from a sample or a JSON context, without sending. | [Email templates](email-templates.md#preview) |
| | `hinoki settings` | Effective settings and their sources, secrets redacted. | [Inspect](inspect.md#settings) |
| | `hinoki openapi` | Print, write or diff the OpenAPI document. | [Inspect](inspect.md#openapi) |
| | `hinoki llms` | Print, write or check the API as llms.txt Markdown for agents. | [Inspect](inspect.md#llms) |
| | `hinoki version` | The Hinoki version; `--verbose` adds Python, platform, native core and Granian. | [Inspect](inspect.md#version) |
| Data | `hinoki migrations plan\|status\|apply\|sql` | Plan and apply migrations for every registered app. | [below](#migrations) |
| | `hinoki admin create-superuser\|set-password` | Manage admin users from the terminal. | [below](#admin) |
| Integrate | `hinoki mcp` | Serve project context to an AI agent over MCP. | [below](#mcp) |
| | `hinoki skills list\|install` | Write the optional agent skills. | [below](#skills) |
| | `hinoki manage [COMMAND]` | Run a command your application defines. | [below](#manage) |
| | `hinoki completion SHELL` | Print a completion script. | [below](#help-and-completion) |

## Targets and short commands

In a project, `hinoki.toml` names the application (`[project] application`), so
commands take no target: `hinoki dev`, `hinoki check`, `hinoki routes`.

To use another application, pass a target, `module:attribute`, imported from
the current directory: `hinoki dev notes.main:app`. A function that returns an
application (`module:create_app`) is also accepted, and `hinoki settings` also
accepts a settings class.

When a target does not import, run the command from the project root (where
`hinoki.toml` is), check that the packages have `__init__.py` files and that the
attribute name is right. The commands report
`error: cannot load notes.main:ap: ...` and exit with a non-zero status.

### Help and completion {#help-and-completion}

`hinoki --help` lists the commands in groups (Start, Inspect, Data, Integrate); `hinoki COMMAND --help` shows one command's options. A mistyped command or migrations action gets a suggestion:

```text
hinoki: error: argument <command>: invalid choice: 'chek' (choose from 'check', ...)
  did you mean 'check'?
```

The completion script is generated from the real parser, so it follows the commands and options of the installed version:

```sh
eval "$(hinoki completion bash)"     # in ~/.bashrc
eval "$(hinoki completion zsh)"      # in ~/.zshrc, after compinit
hinoki completion fish | source      # in ~/.config/fish/config.fish
```

It completes command, subcommand and option names; it does not complete option values or project-specific names such as apps.

### Terminal output

On a colour terminal, `doctor`, `check` and `routes` print a coloured report: a status glyph per check (`✔` ok, `▲` warning, `✘` failed or violated, `?` unverifiable, `–` skipped or waived) with the fix on its own line, and routes with coloured methods. The `check` summary is green only when the exit code is `0`. When output is piped or redirected, or with `NO_COLOR` or `TERM=dumb`, the text is plain and unchanged, and `--format json` is never styled.

## `app` {#app}

The generated `hinoki.toml` registers no app unless `hinoki new` created one (the last question in a terminal, or `--app NAME`): the project package keeps `main.py`, settings, rules and the health route, and models live in apps. It lists the generated rules in `[project] rules = ["NAME.rules:project_rules"]`, so `main.py` passes no `rules=`. Create each one with:

```sh
uv run hinoki app create billing
uv run hinoki app create gateway --no-models
```

This creates `NAME/apps/billing/{models.py,api.py,routes.py,migrations/}` (or omits `models.py` and `migrations/` with `--no-models`) and registers the app in `hinoki.toml`, printing the `Mount("/billing", billing_routes)` snippet to add to `NAME/routes.py` if the app should be served over HTTP. Cross-app foreign keys require the referencing app to list the referenced app in `requires` in `hinoki.toml`; migrations are then planned/applied in dependency order across the full app graph. The project registry is explicit: Hinoki does not scan arbitrary installed packages or run imports at install time.

Both commands report `hinoki.toml`: `hinoki new` lists it among the generated files and `app create` says it registered the app there.

`--no-models` creates an API-only app without persistence, and `--requires` (comma-separated) sets dependencies directly. `app create` writes no events module: to add one, create the module yourself and use `app add --events` or the app's `events` key (see [Events](events.md#declaring-handlers-in-hinokitoml)). Middleware and rules are project-wide lists in the `[project]` table that you edit by hand (see [Middleware](middleware.md#declaring-middleware-in-hinokitoml) and [Rules](rules.md#which-rules-run)). In a terminal, `app create` asks for whatever is not given — the name and, when other apps exist, `requires` — with the same prompts, Esc-to-cancel (exit 130, nothing created) and `NO_COLOR` handling as `hinoki new`. Without a terminal or with `--no-input` it never asks: the name is required (exit 2) and the default is no `requires`.

To register a package that already exists, name its modules. Nothing is created or imported; only `hinoki.toml` changes:

```sh
uv run hinoki app add billing --models legacy.billing.models
uv run hinoki app add invoices --models legacy.invoices.models --migrations legacy.db.invoices --requires billing
uv run hinoki app add gateway --requires billing
```

`--models` is optional: omit both `--models` and `--migrations` to register an API-only app without persistence. When `--models` is given, `--migrations` defaults to the `migrations` package next to the models module (`legacy.billing.migrations`); passing `--migrations` without `--models` is rejected. `--events module:attribute` registers the app's event `Handlers`. The whole manifest is validated before it is replaced, so a duplicate name, an unknown or cyclic `--requires`, or a malformed module name leaves `hinoki.toml` untouched and exits 2. Whether the modules import is checked later, when a command such as `hinoki migrations plan` loads the app.

List the registered apps from the project root or any subdirectory:

```sh
uv run hinoki app list
uv run hinoki app list --format json
```

The text output shows each app in dependency order with its model module and migration package (or `models: none` for API-only apps), the `events` target when set, and prerequisites. JSON contains the same fields (`name`, `models`, `migrations`, `requires`, `events`) under an `apps` key. The command only reads and validates `hinoki.toml`: it does not import app modules, connect to a database or start the app. Without a manifest, it prints an error and exits 2.

## `admin` {#admin}

The admin's own users are managed from the admin itself, except the first one:

```sh
uv run hinoki admin create-superuser ada            # asks for the password twice, no echo
printf '%s\n' "$ADA_PASSWORD" | uv run hinoki admin set-password ada --password-stdin
```

A password is read from a terminal prompt or, with `--password-stdin`, from one line of standard input; it is never taken from the command line, where other users of the machine could read it, and it is never printed. Without a terminal and without the flag the command stops; with the flag on a terminal it still prompts without echo, and a line longer than a password can be is refused unread. Ctrl-C or Ctrl-D at the prompt exits with status `2`, and a refused connection is reported as such, naming the masked database and its source (status `1`), not as missing tables. The database is chosen as for `hinoki migrations`: `--database`, `HINOKI_DATABASE_URL`, the project application's installed `DatabaseModule` or `database` setting, then the project's local SQLite; the command prints it on standard error as `database: … (from …)`, and stops with status `2` when the project's application or database cannot be resolved. The user tables must exist: register and apply them first (`hinoki app add admin ...`, `hinoki migrations apply admin`, see [create the admin's tables](admin.md#create-the-admins-tables) and [who may sign in](admin.md#who-may-sign-in)); otherwise the command exits 1 and says so. Usernames are lower-cased; the shortest password is `admin.min_password_length` of the project application's `admin` settings (the default without a project). Exit status is `0` on success, `2` for a refused input (a taken name, a weak or mistyped password, an unknown user).

## `migrations` {#migrations}

In a project, migration modules come from `hinoki.toml`; you never pass model or
migration paths:

```sh
uv run hinoki migrations plan --check
uv run hinoki migrations plan billing --name add-invoice-index
uv run hinoki migrations status
uv run hinoki migrations apply
uv run hinoki migrations sql 0001-initial
```

`status` and `apply` cover all registered apps in dependency order. Writing a
`plan` in a multi-app project needs the app name (`plan billing`, or
`--app billing`); read-only `plan --check` and `plan --dry-run` without an app
cover every app in dependency order. `sql` searches every app; `--app` narrows
it when two apps share a migration name. `--name` is optional for a
focused plan: Hinoki derives a stable slug from the first planned table change
(for example, `create-table-notes`); pass `--name initial-notes` to override
it. All-app previews cannot use `--name`. Numbering is automatic. `apply`
initializes the configured local SQLite database when no database is configured.
In a manifest project the implicit `.hinoki/local.sqlite3` file, and a relative
SQLite path from the application's settings, are rooted beside `hinoki.toml`,
even if you run the command from a subdirectory; relative SQLite URLs given
with `--database` or `HINOKI_DATABASE_URL` remain relative to the current
working directory. Added
apps have separate migration ledger namespaces, so identical numbered
filenames do not collide; a project whose root package was
registered as an app (older scaffolds) retains its unprefixed migration keys. `apply billing` includes its declared prerequisite
apps for migration application.

`plan --check` fails CI (exit 1) when the models have changes that no migration
file records, and exits 2 when the database it plans for cannot be resolved; it
never connects to a database. In a project it imports the application to read
its database unless `--database` or `HINOKI_DATABASE_URL` is set. Migration
safety across apps, destructive changes and the ledger are explained in
[Migrations](migrations.md).

Without a `hinoki.toml`, name the modules explicitly:

```sh
hinoki migrations --dir myapp/migrations plan --models myapp.models --name add-index --dry-run
hinoki migrations --dir myapp/migrations apply
hinoki migrations --dir myapp/migrations sql 0001-initial
```

`plan` requires `--models MODULE`; `--name` is an optional slug override and is derived from the first planned table change when omitted. `--dry-run` prints operations without writing files. Destructive operations require `--allow-destructive`. plan/status/apply target `--database`, then `HINOKI_DATABASE_URL`, then the `DatabaseModule` or `database` setting of the project's application, then the local SQLite file at `.hinoki/local.sqlite3` ([which database](migrations.md#which-database)); `plan`, `status` and `apply` print the chosen database and its source on standard error with the credentials masked, and stop with exit 2 when the project's application or database cannot be resolved. A URL given as an argument is visible in the process list and shell history, so keep a URL containing a password in the environment. When the connection fails, the command prints `error: database: …` with a `fix:` line and exits 1; the error itself does not repeat the URL. `sql NAME` prints a migration's SQL. Use a disposable database while reviewing migrations.

On an animated terminal, `plan` and `apply` show a spinner while they work and finish with a timing line (`✔ applied   1.2s`); the commands' own output is printed unchanged before it. Piped output, `NO_COLOR`, `TERM=dumb`, `CI` and `HINOKI_ANIMATE=0` print only the commands' own output.

## `mcp` {#mcp}

```sh
uv run hinoki mcp
```

The development server uses MCP over stdio. It accepts `--rules`, repeatable `--package`, repeatable `--models`, and `--waivers` with the same target meanings as `check`. `--rules` defaults to the application's rules and `--package` to the package that contains it, as for [`hinoki check`](inspect.md#check). `--requests PATH` names the file the `recent_requests` tool reads (default `.hinoki/requests.jsonl`; see `server.recent_requests`). `--scenarios off|read|write` controls whether `run_scenario` is registered: `read` allows GET, HEAD, OPTIONS and QUERY; `write` allows every supported method. Scenarios start the real app, run handlers and lifespan hooks, and may write data even for a GET. Keep the tool server owner-controlled and point the app at a disposable database.

The development MCP is separate from application MCP, which is mounted with `expose_as_mcp` and an explicit action allowlist. See the [development MCP guide](dev-mcp.md) for tool inputs and results.

## `skills` {#skills}

```sh
hinoki skills list
hinoki skills install            # from a project directory
hinoki skills install --force    # replace skills that already exist
```

Agent skills are short Markdown playbooks that point an assistant at the real Hinoki tools (the development MCP and the CLI). `list` prints each skill name and description. `install` reads `hinoki.toml`, then writes `.claude/skills/<skill>/SKILL.md` for each one, with the project package name filled in; an existing file is kept and reported unless `--force` replaces it. Without a manifest it prints an error and exits `2`. `hinoki new --skills` writes the same files at creation. They are optional and never written otherwise.

| Skill | Playbook |
| --- | --- |
| `hinoki-add-endpoint` | add an action with its `Schema` types, access policy and test |
| `hinoki-fix-rule` | fix a rule diagnostic without editing the rules or waivers |
| `hinoki-review-api` | run `hinoki openapi --breaking-against` before and after a change |
| `hinoki-test-api` | exercise operations with the generated valid and invalid bodies of `hinoki.ai.samples.request_cases` |

## `manage` {#manage}

```sh
uv run hinoki manage --help
uv run hinoki manage greet Peppe
```

Commands are declared on the app with `Hinoki(commands=[...])`. Each command subclasses `hinoki.management.Command`, gives itself a lower-case name, and adds arguments through `add_arguments(parser)`. In a scaffolded project, `hinoki manage` lists commands and `hinoki manage greet Peppe` runs one; the manifest's `project.application` supplies the app target. Outside a registered project, use `hinoki manage TARGET COMMAND`. `--help` after a command shows its arguments.

```python
# app.py, with `application = "app:app"` in hinoki.toml
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

The Tracker example declares a real command, `Seed`, in
[`examples/tracker/tracker/commands.py`](https://github.com/caspel26/hinoki/blob/main/examples/tracker/tracker/commands.py);
`uv run hinoki manage seed` creates a demo member from its directory, after
`uv run hinoki migrations apply`.

`handle(context, args)` may be synchronous or asynchronous. A sync handler runs on Hinoki's bounded executor. It can read `context.settings` and retrieve resources with `context.get(Type)` or `context.find(Type)`. Declare module classes in the command's `requires` tuple; Hinoki starts those modules and their dependencies for the command, then closes them before it exits. Other app modules are not started. A command with no requirements starts no app modules.

Commands run inside the project process and can modify data. Use disposable databases when developing or testing a command that writes. A handler returns `None` for success or an exit code from 0 through 255. Unknown commands and invalid arguments exit 2; a command failure exits nonzero. App settings load when Hinoki constructs the app, but module resources open only for the selected command.

## Errors and edge cases

- A mistyped command gets a suggestion (`did you mean 'check'?`) and exits 2.
- A target that does not import, or is neither an application nor a factory,
  stops the command with an `error:` line before anything runs.
- Commands that need `hinoki.toml` (`app`, `skills install`) exit 2 without one.
- Interactive prompts (`new`, `app create`) never appear without a terminal or
  with `--no-input`; Ctrl-C or Esc cancels with status 130 and writes nothing.

## See also

- [Create a project](new-project.md)
- [Inspect an application](inspect.md)
- [Serve an application](serving.md)
- [Development MCP](dev-mcp.md)
