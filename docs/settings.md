---
status: implemented
related:
  - serving.md
  - modules.md
---

# Settings: how configuration, secrets and database pools work

Hinoki uses **typed, immutable settings classes**. The declared app form loads its settings class while constructing the app; a factory can call `Settings.load()` itself. Secrets are redacted. Database connection pools have a separate worker lifecycle: settings describe how to connect, and a module opens and closes the pool.

## 1. Declare what your app needs

```python
from typing import Literal
from hinoki import settings

class BlogSettings(settings.AppSettings):          # keeps the built-in groups
    database: settings.DatabaseSettings                   # url: settings.Secret, max_size: int
    jwt_secret: settings.Secret | None = None             # optional secret
    admin_theme: Literal["studio", "console", "paper", "grove"] = "studio"
```

Settings types live in the `hinoki.settings` namespace: `from hinoki import settings`, then `settings.AppSettings`, `settings.DatabaseSettings`, `settings.Secret`, `settings.Settings` and `settings.SettingsError`. Name your own subclass for your project (`BlogSettings` above) and keep a loaded value in a variable such as `app_settings`, so the namespace is not shadowed. Earlier revisions exported these types from `hinoki` itself and called the base class `FrameworkSettings`; `from hinoki import FrameworkSettings` now raises `ImportError`.

`settings.AppSettings` brings five groups every app has: `execution` (8 sync workers, queue limit 64, request timeout 30 seconds, 1 MiB body limit), `lifecycle` (30-second startup and 10-second shutdown limits), `logging` (level, text or JSON), `docs` (title, version and optional OpenAPI/Swagger/ReDoc URLs) and `server` (host, port and workers). A field without a default is **required**. Request IDs and timeout responses are described in the [request lifecycle](request-lifecycle.md).

`docs.openapi_url`, `docs.docs_url` and `docs.redoc_url` default to `None`, so no API document or viewer route is served until configured. Swagger UI or ReDoc requires an `openapi_url`. The same values can be supplied as `Hinoki(...)` arguments; explicit arguments win over `docs` settings, and `None` disables a page.

## Built-in fields and defaults

| Group | Field | Default | Constraint or effect |
| --- | --- | --- | --- |
| `execution` | `sync_workers` | `8` | `1` to `1024` worker threads |
| `execution` | `sync_queue` | `64` | `0` or greater queued calls |
| `execution` | `request_timeout` | `30.0` seconds | Must be positive; request expiry returns `504` |
| `execution` | `max_body_bytes` | `1048576` (1 MiB) | `1` to `1073741824` bytes; larger requests return `413` |
| `lifecycle` | `startup_timeout` | `30.0` seconds | Must be positive |
| `lifecycle` | `shutdown_timeout` | `10.0` seconds | Must be positive |
| `logging` | `level` | `info` | `debug`, `info`, `warning`, `error` or `critical` |
| `logging` | `format` | `text` | `text` or `json` |
| `docs` | `title` | `Hinoki API` | OpenAPI info title |
| `docs` | `version` | `0.1.0` | OpenAPI info version |
| `docs` | `openapi_url` | `None` | JSON document route; disabled when `None` |
| `docs` | `docs_url` | `None` | Swagger UI route; requires `openapi_url` |
| `docs` | `redoc_url` | `None` | ReDoc route; requires `openapi_url` |
| `server` | `host` | `127.0.0.1` | Granian bind address used by `hinoki dev/serve` |
| `server` | `port` | `8000` | `1` to `65535` |
| `server` | `workers` | `1` | `1` to `256`; `dev` uses one worker for reload |
| `server` | `runtime_threads` | `1` | Must be positive |
| `server` | `backlog` | `1024` | Must be positive |
| `server` | `access_log` | `false` | Enables Granian access logging |
| `timezone` | `UTC` | IANA timezone name used by `app.now()` |

`settings.DatabaseSettings` is an optional field you declare in your own settings class, not a `settings.AppSettings` group. Its `url` is required and its `max_size` defaults to `10` connections per worker; the value must be at least `1`.

## 2. Load them when the app is built

```python
class BlogSettings(settings.AppSettings, prefix="BLOG_", dotenv=".env"):  # where to read
    ...

def create_app() -> Hinoki:
    app_settings = BlogSettings.load()
    app = Hinoki(settings=app_settings, ...)
```

`load` is the only place configuration files and environment variables are read. Where a value comes from, highest first:

| Source | Example |
| --- | --- |
| `overrides=` in code (used by tests) | `load(overrides={"admin_theme": "paper"})` |
| the process environment | `BLOG_ADMIN_THEME=paper` |
| a `*_FILE` variable (container secrets) | `BLOG_JWT_SECRET_FILE=/run/secrets/jwt` |
| the `.env` file you named | `BLOG_ADMIN_THEME=paper` in `.env` |
| the TOML file you named | `admin_theme = "paper"` in `settings.toml` |
| the default in the class | `admin_theme: ... = "studio"` |

TOML tables map to settings groups. For example, `[database]` with `max_size = 12` sets `database.max_size`. Arrays remain typed arrays. Environment and dotenv values still use the strict text conversion described below. TOML cannot contain `settings.Secret` values; put secrets in environment variables, `*_FILE` files, or a secret manager that exports one of those sources.

```python
class BlogSettings(settings.AppSettings, prefix="BLOG_", toml="settings.toml"):
    ...

app_settings = BlogSettings.load()  # reads the declared file by default
```

Pass `toml="path/to/settings.toml"` to `load` or `explain` to override the class path. An explicitly selected TOML file must exist and parse correctly.

## 3. Names

The variable is the prefix plus the field name in capitals; a group nests with a double underscore.

| Field | Variable (prefix `BLOG_`) |
| --- | --- |
| `admin_theme` | `BLOG_ADMIN_THEME` |
| `jwt_secret` | `BLOG_JWT_SECRET` |
| `database.url` | `BLOG_DATABASE__URL` |
| `execution.sync_workers` | `BLOG_EXECUTION__SYNC_WORKERS` |
| `logging.format` | `BLOG_LOGGING__FORMAT` |
| `timezone` | `BLOG_TIMEZONE` |

Values are converted strictly: integers are ASCII digits (no `1_000`, no `0x10`), booleans are `true/false/yes/no/on/off/1/0`, lists are comma separated (use `tuple[str, ...]` to keep them immutable), `Literal[...]` takes one of its choices, and an empty value for `X | None` means `None`.

## 4. Mistakes fail at startup, all at once

A variable that starts with your prefix but names no field is an error (a typo guard; `strict=False` turns it off when another program shares the prefix). A missing required value, a bad number or a value that is not one of the choices is an error too, and **every problem is listed together**, with variable names and reasons but never values:

```text
invalid settings:
  BLOG_DATABASE__URL: required, not set
  BLOG_EXECUTION__SYNC_WORKERS: must be an integer
  BLOG_ADMN_THEME: unknown setting
```

## 5. Secrets

* Declare them as `settings.Secret` (or `settings.Secret | None`). A `settings.Secret` prints as `***` in `repr`, `str`, logs, error messages, `describe()` and the settings command; you get the real text only with `secret.reveal()`, at the one place that needs it (`JWTKey(..., settings.jwt_secret.reveal())`).
* Put secrets in the environment, a secret manager that exports variables, or **files**: with Docker or Kubernetes secrets set `BLOG_JWT_SECRET_FILE=/run/secrets/jwt`; the file's content (without the trailing newline) is the value. Only a regular file up to 64 KiB is read; setting both `BLOG_JWT_SECRET` and `BLOG_JWT_SECRET_FILE` is an error.
* A `.env` file is for development; never commit it. Pickling or serialising a settings object stores its secrets in plain text, so don't.
* A `validate()` method you add to a group shows its message in errors, so never put a field's value in it.

## 6. See what is in effect

```sh
hinoki settings blog.settings:BlogSettings --prefix BLOG_ --dotenv .env --toml settings.toml
```

```text
BLOG_ADMIN_THEME              .env         paper
BLOG_DATABASE__URL            environment  ***
BLOG_EXECUTION__SYNC_WORKERS  default      8
BLOG_JWT_SECRET               file         ***
```

The second column is where each value came from: `override`, `environment`, `.env`, `toml`, `file` or `default`. `--format json` prints the same for tools. `--example` prints a ready `.env.example` (every variable with its default and type; secrets and required values blank):

```sh
hinoki settings blog.settings:BlogSettings --prefix BLOG_ --example > .env.example
```

## 7. Use them

`Hinoki(settings=app_settings)` derives the runtime limits, the docs URLs, logging setup and application timezone from the built-in settings (an explicit argument such as `title=` still wins). `app.now()` returns an aware `datetime` in the configured timezone; it defaults to UTC. The timezone is configured in settings, not as a `Hinoki(...)` argument. Your own fields are on `app.settings`, and modules reach them as `ctx.settings` in their lifespan.

The `server` group (host, port, workers) is applied by `hinoki serve`/`dev` ([Running an application](serving.md)). Hinoki does not connect to secret-manager services itself; use a provider that supplies environment values or secret files.

## 8. Database connection ownership

For a server, put the connection details in `settings.DatabaseSettings` and list `DatabaseModule` in the app declaration (or install a configured module in a factory). The module opens a pool in each worker's startup lifespan, fails startup if PostgreSQL is unreachable, and closes that worker's pool on shutdown. It does not connect at module import or when models are imported. Each worker owns its own pool, so budget `replicas × workers × max_size` against the database's connection limit.

```python
class BlogSettings(settings.AppSettings, prefix="BLOG_"):
    database: settings.DatabaseSettings

app = Hinoki(settings=BlogSettings, include=[health, Posts], modules=[DatabaseModule])
```

The configuration is automatic in the useful sense: `BLOG_DATABASE__URL` is loaded from settings and the declared module connects as the server starts. Resource creation and cleanup remain explicit in the module lifecycle; there is no process-global connection opened during import. Factories remain useful when the app's routes or modules depend on runtime choices.

One-off scripts have a different lifecycle. `examples/blog/seed.py` opens a short-lived `Database` explicitly, applies migrations, inserts synthetic rows and closes the pool in `finally`. It runs outside the HTTP server, so there is no worker lifespan to own that resource. This is intentional and avoids starting an app server just to seed a disposable database:

```sh
BLOG_DATABASE_URL=postgres://... uv run python -m examples.blog.seed
```

Use a disposable database for the example. The seed command is not a production migration mechanism and is not run at app startup.
