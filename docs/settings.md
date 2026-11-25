---
status: implemented
related:
  - web-security.md
  - serving.md
  - modules.md
---

# Configure an application with settings

Settings are typed, immutable classes. You declare the fields your application
needs; Hinoki adds the groups every application has, reads values from the
environment, `.env` and TOML once, and refuses to start when something is wrong.

```python
# notes/settings.py
from typing import Literal

from hinoki import settings


class ProjectSettings(settings.AppSettings, prefix="NOTES_", dotenv=".env"):
    database: settings.DatabaseSettings
    support_email: str = "support@example.com"
    stripe_key: settings.Secret | None = None
    theme: Literal["light", "dark"] = "light"
```

`notes/main.py` passes the class with `Hinoki(settings=ProjectSettings)`, which
loads it while building the application. Your fields are then on `app.settings`,
and modules read them as `ctx.settings`.

## Declare fields

A field without a default is required. Use plain types (`str`, `int`, `bool`,
`tuple[str, ...]`, `Literal[...]`, `X | None`), `settings.Secret` for anything
confidential, and a `settings.Settings` subclass for a group of your own. Settings
types live in the `hinoki.settings` namespace (`settings.AppSettings`,
`settings.DatabaseSettings`, `settings.Secret`, `settings.Settings`,
`settings.SettingsError`); name your loaded value `app_settings` so the namespace
is not shadowed.

`settings.AppSettings` brings the built-in groups: `execution`, `lifecycle`,
`logging`, `docs`, `server`, `pagination`, `cors`, `compression`, `metrics`, `security`, `events` and `admin`, plus
`timezone`, `debug` and `allowed_hosts` (see [Hosts, CORS and HTTPS](web-security.md)).
`docs.openapi_url`, `docs.docs_url`, `docs.redoc_url` and `docs.llms_url` default to `None`; the
generated `main.py` passes `openapi_url` and `docs_url` as `Hinoki(...)` arguments, which win over settings; the other two stay off. `llms_url` must start with `/`.

## Built-in fields and defaults

| Group | Field | Default | Constraint or effect |
| --- | --- | --- | --- |
| `execution` | `sync_workers` | `8` | `1` to `1024` worker threads |
| `execution` | `sync_queue` | `64` | `0` or greater queued calls |
| `execution` | `request_timeout` | `30.0` seconds | Must be positive; request expiry returns `504` |
| `execution` | `max_body_bytes` | `1048576` (1 MiB) | `1` to `1073741824` bytes; larger request bodies return `413` |
| `execution` | `max_query_bytes` | `8192` (8 KiB) | `1` to `1048576` raw query-string bytes; larger queries return `414` |
| `execution` | `max_header_bytes` | `32768` (32 KiB) | `1` to `1048576` combined header bytes; larger headers return `431` |
| `execution` | `max_streams` | `64` | `1` or greater; streams open at once per worker, more answer `503` with `Retry-After: 1` before the handler runs |
| `execution` | `stream_idle_timeout` | `None` (uses `request_timeout`) | Must be positive; seconds a `Stream` may stay silent before it is cut |
| `execution` | `sse_keepalive` | `15.0` seconds | Must be positive; quiet seconds before an `EventStream` sends a comment (`keepalive=None` on the stream turns it off) |
| `execution` | `max_websockets` | `1000` | `1` or greater; open [WebSocket](websockets.md) connections per worker (Kiso), more are refused with `503` before the upgrade |
| `execution` | `ws_max_message_bytes` | `1048576` (1 MiB) | `1` to `1073741824`; a larger WebSocket message closes the connection with `1009` |
| `execution` | `ws_queue` | `64` | `1` or greater; messages queued in each direction of a WebSocket (Kiso) |
| `execution` | `ws_ping_interval` | `20.0` seconds | Must be positive, or `None` to send no pings (Kiso) |
| `execution` | `ws_pong_timeout` | `20.0` seconds | Must be positive; a peer that does not answer a ping in time is dropped (Kiso) |
| `execution` | `ws_close_timeout` | `5.0` seconds | Must be positive; how long a WebSocket close handshake may take (Kiso) |
| `lifecycle` | `startup_timeout` | `30.0` seconds | Must be positive |
| `lifecycle` | `shutdown_timeout` | `10.0` seconds | Must be positive |
| `logging` | `level` | `info` | `debug`, `info`, `warning`, `error` or `critical` |
| `logging` | `format` | `text` | `text` or `json` |
| `logging` | `config` | unset | `module:attribute` of a `logging.config.dictConfig` mapping (or a function returning one) for your own loggers, handlers and formatters |
| `logging` | `include_request_id` | `false` | Add the active request ID to Hinoki log records for request correlation |
| `docs` | `title` | `Hinoki API` | OpenAPI info title |
| `docs` | `version` | `0.1.0` | OpenAPI info version |
| `docs` | `openapi_url` | `None` | JSON document route; disabled when `None` |
| `docs` | `docs_url` | `None` | Swagger UI route; requires `openapi_url` |
| `docs` | `redoc_url` | `None` | ReDoc route; requires `openapi_url` |
| `docs` | `llms_url` | `None` | llms.txt route; does not need `openapi_url` |
| `docs` | `openapi_version` | `3.2` | `3.2` or `3.1`: the document's `openapi` version; `Hinoki(openapi_version=...)` overrides it. See [the document version](openapi.md#the-document-version) |
| `server` | `server` | `kiso` | `granian` or `kiso`; `--server` overrides it. See [Kiso server](kiso.md) |
| `server` | `interface` | `asgi` | `asgi` or `wsgi`: what `hinoki serve` calls (a Hinoki application or a WSGI callable); `--interface` overrides it. See [WSGI](serving.md#wsgi) |
| `pagination` | `page_size` | `20` | Page size an action's `PageNumberPagination()` leaves out; `1` to `max_page_size`. See [Pagination](pagination.md) |
| `pagination` | `max_page_size` | `100` | Largest `page_size` a client may ask for |
| `pagination` | `cursor_secret`, `cursor_ttl_seconds` | unset | `settings.Secret` of at least 32 bytes that signs cursors, and an optional lifetime in seconds; see [Pagination](pagination.md) |
| `events` | `handler_timeout` | `5.0` seconds | Must be positive; seconds each handler may run. See [Events](events.md) |
| `events` | `max_handlers` | `16` | `1` or greater; handlers per event and phase |
| `events` | `max_depth` | `4` | `1` or greater; how deep handlers may emit events |
| `server` | `host` | `127.0.0.1` | Bind address used by `hinoki dev/serve` |
| `server` | `port` | `8000` | `1` to `65535` |
| `server` | `workers` | `1` | `1` to `256`; `dev` uses one worker for reload |
| `server` | `runtime_threads` | `1` | Must be positive |
| `server` | `backlog` | `1024` | Must be positive |
| `server` | `access_log` | unset | One line per request on either server. Unset logs in `hinoki dev` only (or whenever `access_log_format` is set); `true` or `false` decides for `dev` and `serve` |
| `server` | `access_log_skip` | unset | Kiso only: paths the log leaves out, exact or a `*` prefix; failures are still logged |
| `server` | `server_timing` | unset | Kiso only: add a `Server-Timing` header with the time taken and the serving lane; unset means `hinoki dev` only, so browser developer tools show it |
| `server` | `sighup` | unset | Kiso only, `hinoki serve`: `"reload"` (also what unset means) replaces the workers one at a time on `SIGHUP`, `"stop"` stops the server instead; see [reload on `SIGHUP`](kiso.md#reload-on-sighup) |
| `server` | `reuse_port` | unset | Kiso only: on Linux, multi-worker `hinoki serve` on a non-zero port gives each worker its own `SO_REUSEPORT` socket; `false` forces one shared listening descriptor everywhere; refused under Granian |
| `server` | `recent_requests` | unset | Kiso only: file that also receives each request as a JSON line for `hinoki mcp`'s `recent_requests` tool; works with the access log off |
| `server` | `access_log_slow_ms` | unset | Kiso only: keep only answers at least this slow (ms); failures are still logged |
| `server` | `access_log_format` | unset | Kiso only (Granian refuses it): `pretty`, `plain`, `json` or `logger` (route to the `hinoki.access` Python logger). Unset is `pretty` on a colour terminal and `plain` otherwise |
| `server` | `max_connections` | unset (10000) | Kiso only: open-connection limit, `1` to `1000000`; refused under Granian |
| `server` | `header_read_timeout` | unset (10 seconds) | Kiso only: time to receive request headers, and the keep-alive idle limit; refused under Granian |
| `server` | `body_read_timeout` | unset (10 seconds) | Kiso only: silence allowed while a body is read (at most half of `execution.request_timeout`); refused under Granian |
| `server` | `write_timeout` | unset (30 seconds) | Kiso only: how long a response write may make no progress before the connection is closed; above 0 and at most 3600; refused under Granian |
| `server` | `native_dispatch` | unset (on) | Kiso only: run the `async def` handlers it can prove safe without an asyncio task (see [the native handler lane](kiso.md#the-native-handler-lane)); `false` keeps every handler in an asyncio task; refused under Granian |
| `server` | `tls_cert_file`, `tls_key_file` | unset | PEM certificate chain and private key; both or neither, and both must exist. Serves HTTPS on either server; Kiso reads them once at start |
| `server` | `http2` | `false` | Serve HTTP/2 over TLS (ALPN `h2`, with `http/1.1` as fallback); needs `tls_cert_file` and `tls_key_file`, otherwise refused when settings load. No cleartext HTTP/2; on either server |
| `server` | `trusted_proxies` | empty | IP addresses or CIDRs allowed to supply forwarded client/protocol metadata; an IPv6 zone id (`fe80::1%lo0`) is refused when settings load, whichever server runs |
| `server` | `request_id_header` | `x-request-id` | HTTP header used for generated request IDs; other framework-owned names are refused |
| `server` | `trust_request_id` | `false` | Accept a validated inbound ID only from a peer in `trusted_proxies` |
| `cors` | `origins` | empty | Allowed browser origins (`scheme://host[:port]`), or the single entry `*`; empty disables CORS |
| `cors` | `methods` | `GET, POST, PUT, PATCH, DELETE, QUERY` | Methods a preflight allows |
| `cors` | `headers` | `authorization, content-type` | Request headers a preflight allows |
| `cors` | `expose_headers` | empty | Response headers a browser script may read |
| `cors` | `credentials` | `false` | Allow cookies and HTTP authentication; refused together with `*` |
| `cors` | `max_age` | `600` seconds | `0` to `86400`; how long a browser may cache a preflight |
| `compression` | `enabled` | `false` | Compress responses with brotli or gzip; Kiso only (`serve --server granian` refuses it). The other `compression` fields are in [compress responses](compression.md#settings) |
| `metrics` | `listen` | unset | `host:port` of the Prometheus metrics listener; Kiso only (`serve --server granian` refuses it). The other `metrics` fields are in [export Prometheus metrics](metrics.md#settings) |
| `admin` | `secret` | unset | The server secret (32+ bytes, from the environment or `*_FILE`) that keys the admin's CSRF tokens; an admin without its own `secret=` needs it |
| `admin` | `session_hours` | `12` | `1` to `720`; a signed-in admin session ends this long after sign-in, whatever else happens |
| `admin` | `idle_minutes` | `120` | `1` to `session_hours * 60`; ... or this long after its last request |
| `admin` | `min_password_length` | `12` | `8` to `128`; the shortest password the admin's own users may have |
| `admin` | `theme` | unset | A built-in admin theme name (`studio`, `console`, `paper`, `bento`) used by an `Admin` without its own `theme=`; an unknown name is refused when the settings load |
| `security` | `redirect_https` | `false` | Send known HTTP requests to HTTPS with `308`; requires explicit `allowed_hosts` |
| `security` | `hsts_seconds` | `0` | `0` to `63072000`; emit HSTS only when the effective scheme is HTTPS |
| `security` | `hsts_include_subdomains` | `false` | Add `includeSubDomains`; requires nonzero `hsts_seconds` |
| `security` | `hsts_preload` | `false` | Add `preload`; requires subdomains and at least one year of HSTS |
| `security` | `frame_options` | `sameorigin` | `settings.FrameOptions.DENY` or `.SAMEORIGIN` |
| `security` | `referrer_policy` | `same-origin` | A supported `settings.ReferrerPolicy` enum value |
| (top level) | `timezone` | `UTC` | IANA timezone name used by `app.now()` |
| (top level) | `debug` | `false` | Adds exception details to `500` answers; development only |
| (top level) | `allowed_hosts` | `*` | Host names, `.domain` or `*`; other `Host` values receive `400` |

`settings.DatabaseSettings` is an optional field you declare in your own settings class, not a `settings.AppSettings` group. Its `url` defaults to `sqlite://./.hinoki/local.sqlite3`; `max_size` defaults to `10` connections per worker and must be at least `1`.

`settings.EmailSettings` is optional in the same way: an `email: settings.EmailSettings` field installs the [email module](email.md). Its `backend` (`smtp`, `console` or `memory`) is required and has no default; `password` is a `Secret` (`NOTES_EMAIL__PASSWORD` or `NOTES_EMAIL__PASSWORD_FILE`). Every field, its default and its range are in [send email](email.md#settings).

## Where values come from

`load` is the only place configuration is read; `Hinoki(settings=ProjectSettings)`
calls it for you. Highest first:

| Source | Example |
| --- | --- |
| `overrides=` in code (tests) | `ProjectSettings.load(overrides={"theme": "dark"})` |
| the process environment | `NOTES_THEME=dark` |
| a `*_FILE` variable (container secrets) | `NOTES_STRIPE_KEY_FILE=/run/secrets/stripe` |
| the `.env` file the class names | `NOTES_THEME=dark` in `.env` |
| the TOML file the class names | `theme = "dark"` in `settings.toml` |
| the default in the class | `theme: ... = "light"` |

Name a TOML file on the class with `toml="settings.toml"` (a project created with
`--server granian` already does). Tables map to groups: `[database]` with
`max_size = 12` sets `database.max_size`. TOML cannot hold `settings.Secret`
values or the database URL; keep those in the environment or `*_FILE` files.
`load(prefix=..., dotenv=..., toml=...)` overrides what the class declared. A
TOML file named on the class or passed to `load` must exist and parse (an empty
file is fine); a missing `.env` is skipped. Relative `.env` and TOML paths
resolve from the current directory, so start the server from the project root.

Load the class yourself when module code needs a typed value before the
application exists:

```python
# notes/main.py
from hinoki import Hinoki

from notes.settings import ProjectSettings

settings = ProjectSettings.load()
app = Hinoki(settings=settings, title=f"Notes ({settings.theme})")
```

Routes, events, middleware and rules come from `hinoki.toml`, so `Hinoki(...)`
needs no `include=`, `events=` or `rules=` here.

## Variable names

The variable is the prefix plus the field name in capitals; a group nests with a
double underscore.

| Field | Variable |
| --- | --- |
| `support_email` | `NOTES_SUPPORT_EMAIL` |
| `stripe_key` | `NOTES_STRIPE_KEY` |
| `database.url` | `NOTES_DATABASE__URL` |
| `execution.sync_workers` | `NOTES_EXECUTION__SYNC_WORKERS` |
| `logging.format` | `NOTES_LOGGING__FORMAT` |

Values are converted strictly: integers are ASCII digits (no `1_000`, no `0x10`), booleans are `true/false/yes/no/on/off/1/0`, lists are comma separated (use `tuple[str, ...]` to keep them immutable), `Literal[...]` takes one of its choices, and an empty value for `X | None` means `None`.

## Mistakes fail at startup, all at once

A variable that starts with your prefix and looks like one of your settings but names no field is an error (a typo guard; `strict=False` turns it off). It looks like a setting when its first word starts a field's variable (`NOTES_DATABASE_URL`, `NOTES_SERVER__PROT`) or when it is spelt close to one (`NOTES_THEMES`). Other variables that only share the prefix belong to another program and are ignored: systemd's `JOURNAL_STREAM` does not break a project whose prefix is `JOURNAL_`. A missing required value, a bad number or a value that is not one of the choices is an error too, and **every problem is listed together**, with variable names and reasons but never values:

```text
invalid settings:
  NOTES_EXECUTION__SYNC_WORKERS: must be an integer
  NOTES_DATABASE__MAX_SIZE: must be an integer
  NOTES_THEMES: unknown setting
```

## Keep secrets secret

* Declare them as `settings.Secret` (or `settings.Secret | None`). A `settings.Secret` (the same type as `hinoki.Secret`) prints as `**********` in `repr`, `str`, logs and error messages, and as `***` in `describe()` and the settings command; you get the real text only with `secret.reveal()`, at the one place that needs it (where the payment client is built: `app_settings.stripe_key.reveal()`).
* Put secrets in the environment, a secret manager that exports variables, or **files**: with Docker or Kubernetes secrets set `NOTES_STRIPE_KEY_FILE=/run/secrets/stripe`; the file's content (without the trailing newline) is the value. Only a regular file up to 64 KiB is read; setting both `NOTES_STRIPE_KEY` and `NOTES_STRIPE_KEY_FILE` is an error.
* A `.env` file is for development; never commit it. Pickling or serialising a settings object stores its secrets in plain text, so don't.
* A `validate()` method you add to a group shows its message in errors, so never put a field's value in it.

## See what is in effect

```sh
hinoki settings
```

```text
NOTES_DATABASE__URL            environment  ***
NOTES_EXECUTION__SYNC_WORKERS  default      8
NOTES_STRIPE_KEY               file         ***
NOTES_THEME                    .env         dark
```

The second column is where each value came from: `override`, `environment`,
`.env`, `toml`, `file` or `default`. The command reads the prefix, `.env` and
TOML file the class declares; `--prefix`, `--dotenv` and `--toml` override them.
`--format json` prints the same for tools, and `--example` writes a ready
`.env.example`: every variable with its default and type, required values blank
to fill in, and the optional ones with no printable default (secrets such as the
database URL, values that default to unset) commented out, so a copied file
keeps their defaults:

```sh
hinoki settings --example > .env.example
```

## Logging and your own loggers {#your-own-loggers}

Declare loggers, handlers and formatters with a standard [`logging.config.dictConfig`](https://docs.python.org/3/library/logging.config.html#logging-config-dictschema) mapping, and point `logging.config` at it:

```python
# notes/logs.py
LOGGING = {
    "version": 1,
    "formatters": {"short": {"format": "%(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "short"}},
    "loggers": {"orders": {"handlers": ["console"], "level": "INFO"}},
}
```

```sh
NOTES_LOGGING__CONFIG=notes.logs:LOGGING
```

The value may also name a function that returns the mapping. It is applied when each worker starts, right after Hinoki's own `hinoki` logger setup, with `disable_existing_loggers` off unless your mapping sets it. A mapping that configures the `hinoki` logger replaces Hinoki's handler on it. A target that cannot be imported or is not a mapping raises during startup. Kiso's access log is written by Kiso; set `server.access_log_format = "logger"` to route it through the `hinoki.access` logger.

## Database connection ownership {#8-database-connection-ownership}

A `database: settings.DatabaseSettings` field is all it takes: Hinoki installs
`DatabaseModule` for it (a module declared in `modules=` replaces it). With
nothing configured, the URL is the local SQLite file
`sqlite://./.hinoki/local.sqlite3`, and the module creates its hidden directory
and file when it starts. [Configure a database](databases.md) covers the URL,
the connection fields, TLS and each backend.

The module opens a pool in each worker's startup lifespan, fails startup if the
database is unreachable, and closes that worker's pool on shutdown. Nothing
connects when modules or models are imported. Each worker owns its own pool,
so budget `replicas × workers × max_size` against the server database's
connection limit.

One-off scripts have a different lifecycle: they open a short-lived `Database`
themselves ([use the database from a script](databases.md#use-the-database-from-a-script)).
`examples/blog/blog/apps/blog/seed.py` does so, applies migrations, inserts synthetic rows and
closes the pool in `finally`. It is a one-time seed for a fresh disposable
database, not a repeatable reset:

```sh
cd examples/blog
export BLOG_DATABASE__URL=postgresql://app:password@localhost/blog
uv run python -m blog.apps.blog.seed
```

Use a disposable database for the example. The seed command is not a production migration mechanism and is not run at app startup.

## Errors and edge cases

- Setting both `NOTES_STRIPE_KEY` and `NOTES_STRIPE_KEY_FILE` is an error; only a regular file up to 64 KiB is read.
- A variable with your prefix that looks like a setting (same first word, or a close spelling) but names no field is an error; other variables that only share the prefix, such as `JOURNAL_STREAM`, are ignored. Pass `strict=False` to `load` to turn the check off.
- The `server` group is applied by `hinoki dev` and `hinoki serve`, not by other ASGI servers ([Serving](serving.md#settings-that-apply-to-one-server-only)).

## See also

- [Hosts, CORS and HTTPS](web-security.md)
- [Own resources with modules](modules.md)
- [Configure a database](databases.md#configure-the-application)
