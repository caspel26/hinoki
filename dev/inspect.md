# Inspect an application

These commands load the application, report and exit. None of them serves
requests, opens a database or writes files (except `openapi --output`):

```sh
uv run hinoki doctor
uv run hinoki routes
```

Each accepts `--format json` for scripts. On a colour terminal the reports use
`✔` ok, `▲` warning, `✘` failed and `–` skipped; piped output, `NO_COLOR` and
`TERM=dumb` give plain text.

## Diagnose a project with `doctor` {#doctor}

```sh
uv run hinoki doctor
uv run hinoki doctor --format json
```

With no target, `doctor` uses `project.application` from `hinoki.toml`. If there
is no manifest or default target, it reports environment checks and marks app
checks as skipped. An explicit `TARGET` names another app (`module:app`).
Loading it imports your Python code, so module-level code may have its
own side effects. Hinoki does not start app or module lifespans, run hooks,
start a server, run architecture rules, connect to a database, or write files.

The report covers manifest validity, Python/native extension and optional
Granian availability, route registration, configured typed settings, and
in-memory OpenAPI generation. An app with no settings object reports that
check as skipped; a custom non-`Settings` object is a warning. JSON output has
`schema_version: 1` and stable check IDs. Settings values are not included.
When the application cannot be loaded, the finding names the exception class;
for a Hinoki declaration error with no chained cause (for example a missing
admin secret) it also shows the message, on one line and truncated. Other
exception messages are not included. Exit status is `0` when no
diagnostic failed, `1` for invalid manifest or app diagnostics, and `2` when a
requested target cannot be loaded. Warnings and skipped checks remain visible.

Database connectivity and applied migration state are intentionally separate:
use [`hinoki migrations status`](https://hinoki.caspel.dev/dev/migrations.md) for the live ledger and
[`hinoki migrations plan --check`](https://hinoki.caspel.dev/dev/migrations.md#check-migration-drift) for
model-to-file drift.

## Check the rules with `check` {#check}

```sh
uv run hinoki check
uv run hinoki check --phase static
```

| Option | Effect | Default |
| --- | --- | --- |
| `--rules MODULE:ATTRIBUTE` | the `RuleSet` to run | the application's own rules: `Hinoki(rules=...)`, else the `[project] rules` of `hinoki.toml` ([which rules run](https://hinoki.caspel.dev/dev/rules.md#which-rules-run)) |
| `--package PACKAGE` | package for the static import checks; repeat for several | the package that contains the target |
| `--phase startup\|static` | run one phase | both |
| `--waivers PATH` | waiver file; must exist | the app's `waivers=`, else `hinoki-rules.toml` at the project root ([which file](https://hinoki.caspel.dev/dev/rules.md#which-waiver-file)) |
| `--format text\|json` | human or versioned JSON output | `text` |
| `--allow-effects` | also run checks declared effectful (files, network, database) | off |

Exit status is `0` when no rule fails, `1` for violations and `2` when the target
or the checks cannot run. An application with no rules passes with `0 rule(s)` and
a warning on standard error, since nothing was checked. What each rule checks, and
how to read and waive a result, is in [Rules](https://hinoki.caspel.dev/dev/rules.md).

## List routes with `routes` {#routes}

```sh
uv run hinoki routes
```

From `examples/tracker`:

```text
GET     /activity                  MemberTokens[tracker:use] tracker.apps.activity.api.Feed.list
GET     /activity/{item_id}        MemberTokens[tracker:use] tracker.apps.activity.api.Feed.retrieve
GET     /health                    public                    tracker.routes.health
GET     /members/me                MemberTokens[tracker:use] tracker.apps.members.api.me
GET     /tasks                     MemberTokens[tracker:use] tracker.apps.tasks.api.Tasks.list
POST    /tasks                     MemberTokens[tracker:use] tracker.apps.tasks.api.Tasks.create
DELETE  /tasks/{item_id}           MemberTokens[tracker:use] tracker.apps.tasks.api.Tasks.delete
GET     /tasks/{item_id}           MemberTokens[tracker:use] tracker.apps.tasks.api.Tasks.retrieve
POST    /tasks/{item_id}/complete  MemberTokens[tracker:use] tracker.apps.tasks.api.Tasks.complete
```

Each row is method, path, access (the provider and its permissions, or `public`)
and handler, sorted by path. The handler is the action's rule subject, the name
that `except_` and waivers match: an action of an `API` class is named after the
class it is mounted on, even when it inherits the method. `--path` keeps paths
that start with the prefix, `--method` one method; no match prints
`no routes match` and exits 1:

```sh
uv run hinoki routes --path /tasks --method GET
```

```text
GET     /tasks            MemberTokens[tracker:use] tracker.apps.tasks.api.Tasks.list
GET     /tasks/{item_id}  MemberTokens[tracker:use] tracker.apps.tasks.api.Tasks.retrieve
```

`--lanes` adds, per route, whether [Kiso](https://hinoki.caspel.dev/dev/kiso.md#the-native-handler-lane) runs
the handler natively (`native`) or through the Python pipeline (`python`), with
the reason. The access log's `lane` field means something narrower: a request
for a `native` route here still logs `python` unless Rust runs a generated CRUD
plan for it. JSON always has `method`, `path`, `handler`, `access`, `status`,
`lane` and `lane_reason`.

## List event handlers with `events` {#events}

```sh
uv run hinoki events
uv run hinoki events notes.main:app --format json
```

Prints each registered [event handler](https://hinoki.caspel.dev/dev/events.md#seeing-what-is-registered) in
execution order (event, phase, collection, `async` or `sync`, handler), the
`events` limits, and the `events` target each app declares in `hinoki.toml`.
`--format json` has `handlers`, `limits` and `apps`. It reads the application
and the manifest only; an unloadable target exits 2.

## Export the OpenAPI document with `openapi` {#openapi}

```sh
uv run hinoki openapi --output openapi.json
uv run hinoki openapi --check openapi.json
uv run hinoki openapi --breaking-against openapi-baseline.json
```

The command builds the app without serving it or opening a database; it works even when `openapi_url` is disabled. With no file option it prints deterministic JSON (sorted keys, two-space indentation and a trailing newline). `--output FILE` writes those bytes. `--check FILE` compares parsed JSON, so formatting differences do not count as drift. Exit status is `0` when the file matches or is written, `1` when its JSON differs, and `2` when the target cannot load or the check file is missing or invalid JSON. `--output` and `--check` cannot be used together.

`--breaking-against BASELINE` compares the generated document with an earlier OpenAPI JSON file (the released version) and prints one `breaking: …` line per change that would break an existing client.

Reported: a removed operation or success response, new or newly required request input (a parameter that becomes required, a required request body where there was none), a removed response property or JSON body, a changed type (parameters included), a changed type, and a request enum that narrows or a response enum that widens. Additions are compatible and are not reported. Exit status is `0` when nothing breaks, `1` when something does, and `2` when the target cannot load or the baseline is missing or not a JSON object. It cannot be combined with `--output` or `--check`. The comparison reads the documents only; it does not check behavior that an OpenAPI document does not describe.

Run `--check` in CI to catch an OpenAPI document that drifted from the code, and
`--breaking-against` with the last released document before a release. See
[OpenAPI](https://hinoki.caspel.dev/dev/openapi.md) for what the document contains.

## Render llms.txt with `llms` {#llms}

```sh
uv run hinoki llms
uv run hinoki llms --output llms.txt
uv run hinoki llms --check llms.txt
```

Renders the application's OpenAPI document as an [llms.txt](https://hinoki.caspel.dev/dev/openapi.md#llms) Markdown text: a title, the version, then the operations grouped by first tag with their summary, deprecation, security scheme names and request and response components. Like `openapi`, it builds the app without serving it or opening a database and works when `openapi_url` is disabled; an action with `include_in_schema=False` is not listed. With no file option it prints the text; `--output FILE` writes the same text as UTF-8 with LF line endings on every platform, and `--check FILE` compares it exactly, except that a CRLF line ending in the file (a Windows checkout with `autocrlf`) counts as LF. Exit status is `0` when the file matches or is written, `1` when it differs, and `2` when the target cannot load or the check file is missing or unreadable, or the output cannot be written. `--output` and `--check` cannot be used together.

## Show settings with `settings` {#settings}

```sh
uv run hinoki settings
uv run hinoki settings --format json
uv run hinoki settings --example
```

The target is an application with typed settings or a settings class
(`notes.settings:ProjectSettings`). Each value is shown with where it came from;
`settings.Secret` values are redacted. `--prefix`, `--dotenv` and `--toml`
replace the sources the class declares. `--example` prints a `.env.example` with
defaults and types, leaving required fields blank and commenting out optional
values with no printable default (secrets, unset values). See
[Settings](https://hinoki.caspel.dev/dev/settings.md#see-what-is-in-effect).

## Report versions with `version` {#version}

```sh
uv run hinoki version --verbose
```

The default prints `hinoki X.Y.Z`. `--verbose` adds the Python version, platform, native extension path and Granian version; Granian is reported as `not installed` when its optional package is absent. Use `--format json` for machine-readable output. Details the binding does not expose, including its bundled SQLite library version, are omitted rather than inferred.

## Errors and edge cases

- A target that does not import prints an `error:` line with the import failure
  (`error: cannot load TARGET: …` for `routes` and `openapi`) and exits
  non-zero (2 for `check`, `routes` and `openapi`).
- Importing the target runs your module-level code; keep import-time
  side effects out of it.
- `openapi` options `--output`, `--check` and `--breaking-against` exclude each
  other.
- `doctor` without `hinoki.toml` still checks the environment and marks the
  application checks skipped.

## See also

- [Rules](https://hinoki.caspel.dev/dev/rules.md)
- [OpenAPI](https://hinoki.caspel.dev/dev/openapi.md)
- [Troubleshooting](https://hinoki.caspel.dev/dev/troubleshooting.md)
- [Command line](https://hinoki.caspel.dev/dev/cli.md)
