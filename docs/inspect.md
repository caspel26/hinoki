---
status: implemented
related:
  - rules.md
  - openapi.md
  - troubleshooting.md
---

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
`schema_version: 1` and stable check IDs. Settings values and exception
messages are not included. Exit status is `0` when no
diagnostic failed, `1` for invalid manifest or app diagnostics, and `2` when a
requested target cannot be loaded. Warnings and skipped checks remain visible.

Database connectivity and applied migration state are intentionally separate:
use [`hinoki migrations status`](migrations.md) for the live ledger and
[`hinoki migrations plan --check`](migrations.md#check-migration-drift) for
model-to-file drift.

## Check the rules with `check` {#check}

```sh
uv run hinoki check
uv run hinoki check --phase static
```

| Option | Effect | Default |
| --- | --- | --- |
| `--rules MODULE:ATTRIBUTE` | the `RuleSet` to run | the application's `rules=` |
| `--package PACKAGE` | package for the static import checks; repeat for several | the package that contains the target |
| `--phase startup\|static` | run one phase | both |
| `--waivers PATH` | waiver file; must exist | the app's `waivers=`, else `hinoki-rules.toml` at the project root ([which file](rules.md#which-waiver-file)) |
| `--format text\|json` | human or versioned JSON output | `text` |
| `--allow-effects` | also run checks declared effectful (files, network, database) | off |

Exit status is `0` when no rule fails, `1` for violations and `2` when the target
or the checks cannot run. What each rule checks, and how to read and waive a
result, is in [Rules](rules.md).

## List routes with `routes` {#routes}

```sh
uv run hinoki routes
```

```text
GET     /billing/invoices               StaffTokens[billing:manage] notes.apps.billing.api.list_invoices
POST    /billing/invoices               StaffTokens[billing:manage] notes.apps.billing.api.create_invoice
GET     /billing/invoices/{invoice_id}  StaffTokens[billing:manage] notes.apps.billing.api.get_invoice
GET     /health                         public                   notes.api.health
```

Each row is method, path, access (the provider and its permissions, or `public`)
and handler, sorted by path. `--path` keeps paths that start with the prefix,
`--method` one method; no match prints `no routes match` and exits 1:

```sh
uv run hinoki routes --path /billing --method GET
```

```text
GET     /billing/invoices               StaffTokens[billing:manage] notes.apps.billing.api.list_invoices
GET     /billing/invoices/{invoice_id}  StaffTokens[billing:manage] notes.apps.billing.api.get_invoice
```

`--lanes` adds, per route, whether [Kiso](kiso.md#the-native-handler-lane) runs
the handler natively (`native`) or through the Python pipeline (`python`), with
the reason. JSON always has `method`, `path`, `handler`, `access`, `status`,
`lane` and `lane_reason`.

## Export the OpenAPI document with `openapi` {#openapi}

```sh
uv run hinoki openapi --output openapi.json
uv run hinoki openapi --check openapi.json
uv run hinoki openapi --breaking-against openapi-baseline.json
```

The command builds the app without serving it or opening a database; it works even when `openapi_url` is disabled. With no file option it prints deterministic JSON (sorted keys, two-space indentation and a trailing newline). `--output FILE` writes those bytes. `--check FILE` compares parsed JSON, so formatting differences do not count as drift. Exit status is `0` when the file matches or is written, `1` when its JSON differs, and `2` when the target cannot load or the check file is missing or invalid JSON. `--output` and `--check` cannot be used together.

`--breaking-against BASELINE` compares the generated document with an earlier OpenAPI JSON file (the released version) and prints one `breaking: …` line per change that would break an existing client.

Reported: a removed operation or success response, new or newly required request input, a removed response property, a changed type, and a request enum that narrows or a response enum that widens. Additions are compatible and are not reported. Exit status is `0` when nothing breaks, `1` when something does, and `2` when the target cannot load or the baseline is missing or not a JSON object. It cannot be combined with `--output` or `--check`. The comparison reads the documents only; it does not check behavior that an OpenAPI document does not describe.

Run `--check` in CI to catch an OpenAPI document that drifted from the code, and
`--breaking-against` with the last released document before a release. See
[OpenAPI](openapi.md) for what the document contains.

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
[Settings](settings.md#see-what-is-in-effect).

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

- [Rules](rules.md)
- [OpenAPI](openapi.md)
- [Troubleshooting](troubleshooting.md)
- [Command line](cli.md)
