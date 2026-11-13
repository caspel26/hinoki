# Troubleshooting

## `cannot load module:attribute`

Run the command from the application checkout and check both the module path and attribute spelling. `hinoki` adds the current directory when importing targets, but the package still needs to be importable Python code.

## Settings report a missing or unknown variable

Check the settings class's `prefix=` and nested field names. Nested groups use `__`, for example `APP_DATABASE__URL`. Run `hinoki settings module:SettingsClass --example` to inspect expected names. Secret values are redacted by design.

## Database connection fails at startup

The `DatabaseModule` opens a pool in each worker's startup. Confirm the URL, network reachability and PostgreSQL availability from the same environment as the worker. Use one worker and a small pool while debugging; total maximum connections are replicas × workers × pool size.

## A related field is not loaded

Foreign-key attributes do not trigger queries. Add `select_related("author")` or `prefetch_related("posts")` to the query that needs the relation.

## `hinoki check` reports `unverifiable`

The rule could not establish its promise. Read the diagnostic explanation and any `fix:` line. Some rules inspect only declarations; effectful checks require the explicit `--allow-effects` flag.

## A sync handler or auth provider stalls

Sync code runs on a bounded executor and can saturate. Prefer async database and network calls in async handlers. See [sync and async](SYNC-ASYNC.md) for limits and [server settings](SERVING.md) for worker configuration.

## A migration is marked destructive

Review the generated SQL and the data it may remove. Test the change against a disposable database backup before using `--allow-destructive`. See [migrations](MIGRATIONS.md).

If the issue persists, include the Hinoki version, the command or route, the complete diagnostic, and a minimal example. Remove credentials and user data first.
