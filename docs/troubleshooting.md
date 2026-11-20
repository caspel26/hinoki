---
status: implemented
related:
  - cli.md
  - serving.md
  - settings.md
---

# Troubleshooting

Each section starts with the message or symptom you see.

## `error: cannot load notes.main:app`

Run the command from the project directory (the one with `hinoki.toml`) and check the module path and the attribute after the colon. Hinoki adds the current directory to the import path, but the module must still import cleanly: the text after the colon is the original exception.

## Settings report a missing or unknown variable

Check the settings class's `prefix=` and the nested names: groups are joined with `__`, as in `NOTES_DATABASE__URL`. Print every expected name with:

```sh
hinoki settings notes.main:app --example
```

`settings.Secret` values are shown redacted on purpose.

## The database connection fails at startup

Each worker opens its pool during startup. Check the backend URL, network reachability and the server from the same environment as the worker, and the backend notes in [configure a database](databases.md). While debugging, use one worker and a small pool: total connections are replicas × workers × pool size.

## `server.<name> is not supported by the granian server`

`hinoki serve` refuses a setting the chosen server cannot honour, and exits with status 2. Unset it or run on Kiso, the default. See [settings that apply to one server only](serving.md#settings-that-apply-to-one-server-only).

## `cannot listen on HOST:PORT`

The address is in use, or the host or port is invalid. Stop the other process or pass another `--port`; the command exits with status 2.

## Every request answers `400 disallowed_host` or `503 not_ready`

`400 disallowed_host`: the `Host` header is not in [`allowed_hosts`](web-security.md#allowed_hosts). `503 not_ready`: the worker has not finished starting, or has begun shutting down ([startup probe](serving.md#optional-startup-probe)).

## `RelationNotLoaded`

Relations are never loaded lazily. Name the relation in the query that needs it: `select_related("author")` for a single row, `prefetch_related("posts")` for a collection. See [relations](relations.md).

## `hinoki check` reports `unverifiable`

The rule could not establish its promise. Read the explanation and any `fix:` line. Some rules inspect only declarations; checks with side effects run only with `hinoki check notes.main:app --allow-effects`.

## Sync handlers answer `503 overloaded`

Sync handlers and sync hooks share a bounded pool and queue. Move database and network calls to async handlers, keep sync hooks short, or size the pool; see [sync and async code](sync-async.md#size-the-pool).

## A migration is marked destructive

Read the generated SQL and the data it removes, and test it against a disposable copy before passing `--allow-destructive`. See [migrations](migrations.md).

## Report a problem

Collect the environment without reading settings or opening a database:

```sh
hinoki version --verbose --format json
hinoki doctor
```

`hinoki doctor` imports the project application but does not start it, run rules, connect to a database or write files. Include the Hinoki version, the command or route, the complete diagnostic and a minimal example; remove credentials and personal data first.
