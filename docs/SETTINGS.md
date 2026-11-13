# Settings: how configuration and secrets work

Hinoki has one way to configure an application: a **typed, immutable settings class** that you load once, explicitly, when the app is built. Nothing is read from the environment by magic, and nothing secret is ever printed.

## 1. Declare what your app needs

```python
from typing import Literal
from hinoki import DatabaseSettings, FrameworkSettings, Secret

class BlogSettings(FrameworkSettings):          # keeps the built-in groups
    database: DatabaseSettings                   # url: Secret, max_size: int
    jwt_secret: Secret | None = None             # optional secret
    admin_theme: Literal["studio", "midnight", "paper"] = "studio"
```

`FrameworkSettings` brings four groups every app has: `execution` (sync workers, queue, request timeout, body limit), `lifecycle` (startup and shutdown timeouts), `logging` (level, text or JSON) and `docs` (title, version, OpenAPI/Swagger/ReDoc URLs). A field without a default is **required**.

## 2. Load them when the app is built

```python
class BlogSettings(FrameworkSettings, prefix="BLOG_", dotenv=".env"):  # where to read
    ...

def create_app() -> HinokiAPI:
    settings = BlogSettings.load()
    app = HinokiAPI(settings=settings, ...)
```

`load` is the only place variables are read. Where a value comes from, highest first:

| Source | Example |
| --- | --- |
| `overrides=` in code (used by tests) | `load(overrides={"admin_theme": "paper"})` |
| the process environment | `BLOG_ADMIN_THEME=paper` |
| a `*_FILE` variable (container secrets) | `BLOG_JWT_SECRET_FILE=/run/secrets/jwt` |
| the `.env` file you named | `BLOG_ADMIN_THEME=paper` in `.env` |
| the default in the class | `admin_theme: ... = "studio"` |

## 3. Names

The variable is the prefix plus the field name in capitals; a group nests with a double underscore.

| Field | Variable (prefix `BLOG_`) |
| --- | --- |
| `admin_theme` | `BLOG_ADMIN_THEME` |
| `jwt_secret` | `BLOG_JWT_SECRET` |
| `database.url` | `BLOG_DATABASE__URL` |
| `execution.sync_workers` | `BLOG_EXECUTION__SYNC_WORKERS` |
| `logging.format` | `BLOG_LOGGING__FORMAT` |

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

* Declare them as `Secret` (or `Secret | None`). A `Secret` prints as `***` in `repr`, `str`, logs, error messages, `describe()` and the settings command; you get the real text only with `secret.reveal()`, at the one place that needs it (`JWTKey(..., settings.jwt_secret.reveal())`).
* Put secrets in the environment, a secret manager that exports variables, or **files**: with Docker or Kubernetes secrets set `BLOG_JWT_SECRET_FILE=/run/secrets/jwt`; the file's content (without the trailing newline) is the value. Only a regular file up to 64 KiB is read; setting both `BLOG_JWT_SECRET` and `BLOG_JWT_SECRET_FILE` is an error.
* A `.env` file is for development; never commit it. Pickling or serialising a settings object stores its secrets in plain text, so don't.
* A `validate()` method you add to a group shows its message in errors, so never put a field's value in it.

## 6. See what is in effect

```sh
python -m hinoki settings blog.settings:BlogSettings --prefix BLOG_ --dotenv .env
```

```text
BLOG_ADMIN_THEME              .env         paper
BLOG_DATABASE__URL            environment  ***
BLOG_EXECUTION__SYNC_WORKERS  default      8
BLOG_JWT_SECRET               file         ***
```

The second column is where each value came from: `override`, `environment`, `.env`, `file` or `default`. `--format json` prints the same for tools. `--example` prints a ready `.env.example` (every variable with its default and type; secrets and required values blank):

```sh
python -m hinoki settings blog.settings:BlogSettings --prefix BLOG_ --example > .env.example
```

## 7. Use them

`HinokiAPI(settings=settings)` derives the runtime limits, the docs URLs and the logging setup from the built-in groups (an explicit argument such as `title=` still wins). Your own fields are on `app.settings`, and modules reach them as `ctx.settings` in their lifespan.

The `server` group (host, port, workers) is applied by `python -m hinoki serve`/`dev` ([Running an application](SERVING.md)). Not included yet: TOML files and secret-manager clients as sources.
