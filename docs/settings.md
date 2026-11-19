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
    admin_theme: Literal["studio", "console", "paper", "bento"] = "studio"
```

Settings types live in the `hinoki.settings` namespace: `from hinoki import settings`, then `settings.AppSettings`, `settings.DatabaseSettings`, `settings.Secret`, `settings.Settings` and `settings.SettingsError`. Name your own subclass for your project (`BlogSettings` above) and keep a loaded value in a variable such as `app_settings`, so the namespace is not shadowed. Earlier revisions exported these types from `hinoki` itself and called the base class `FrameworkSettings`; `from hinoki import FrameworkSettings` now raises `ImportError`.

`settings.AppSettings` brings eight groups every app has: `execution` (8 sync workers, queue limit 64, request timeout 30 seconds, 1 MiB body limit, 8 KiB query limit and 32 KiB header limit), `lifecycle` (30-second startup and 10-second shutdown limits), `logging` (level, text or JSON, optional request ID correlation), `docs` (title, version and optional OpenAPI/Swagger/ReDoc URLs), `server` (which server, host, port, workers and proxy trust), `pagination` (default `page_size` and `max_page_size` for `PageNumberPagination`, see [pagination](pagination.md#defaults-for-the-whole-application)), `cors` (which browser origins may read responses) and `security` (browser response headers and optional HTTPS redirect). Three values sit beside the groups: `timezone`, `debug` and `allowed_hosts`; see [Debug, allowed hosts and CORS](#debug-allowed-hosts-and-cors). A field without a default is **required**. Request IDs and timeout responses are described in the [request lifecycle](request-lifecycle.md).

`docs.openapi_url`, `docs.docs_url` and `docs.redoc_url` default to `None`, so no API document or viewer route is served until configured. Swagger UI or ReDoc requires an `openapi_url`. The same values can be supplied as `Hinoki(...)` arguments; explicit arguments win over `docs` settings, and `None` disables a page.

## Built-in fields and defaults

| Group | Field | Default | Constraint or effect |
| --- | --- | --- | --- |
| `execution` | `sync_workers` | `8` | `1` to `1024` worker threads |
| `execution` | `sync_queue` | `64` | `0` or greater queued calls |
| `execution` | `request_timeout` | `30.0` seconds | Must be positive; request expiry returns `504` |
| `execution` | `max_body_bytes` | `1048576` (1 MiB) | `1` to `1073741824` bytes; larger request bodies return `413` |
| `execution` | `max_query_bytes` | `8192` (8 KiB) | `1` to `1048576` raw query-string bytes; larger queries return `414` |
| `execution` | `max_header_bytes` | `32768` (32 KiB) | `1` to `1048576` combined header bytes; larger headers return `431` |
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
| `server` | `server` | `kiso` | `granian` or `kiso`; `--server` overrides it. See [Running an application](serving.md#the-rust-server) |
| `server` | `host` | `127.0.0.1` | Bind address used by `hinoki dev/serve` |
| `server` | `port` | `8000` | `1` to `65535` |
| `server` | `workers` | `1` | `1` to `256`; `dev` uses one worker for reload |
| `server` | `runtime_threads` | `1` | Must be positive |
| `server` | `backlog` | `1024` | Must be positive |
| `server` | `access_log` | unset | One line per request on either server. Unset logs in `hinoki dev` only (or whenever `access_log_format` is set); `true` or `false` decides for `dev` and `serve` |
| `server` | `access_log_skip` | unset | Kiso only: paths the log leaves out, exact or a `*` prefix; failures are still logged |
| `server` | `access_log_slow_ms` | unset | Kiso only: keep only answers at least this slow (ms); failures are still logged |
| `server` | `access_log_format` | unset | Kiso only (Granian refuses it): `pretty`, `plain`, `json` or `logger` (route to the `hinoki.access` Python logger). Unset is `pretty` on a colour terminal and `plain` otherwise |
| `server` | `max_connections` | unset (10000) | Rust server only: open-connection limit, `1` to `1000000`; refused under Granian |
| `server` | `header_read_timeout` | unset (10 seconds) | Rust server only: time to receive request headers, and the keep-alive idle limit; refused under Granian |
| `server` | `body_read_timeout` | unset (10 seconds) | Rust server only: silence allowed while a body is read (at most half of `execution.request_timeout`); refused under Granian |
| `server` | `write_timeout` | unset (30 seconds) | Rust server only: how long a response write may make no progress before the connection is closed; above 0 and at most 3600; refused under Granian |
| `server` | `native_dispatch` | unset (on) | Rust server only: run the `async def` handlers it can prove safe without an asyncio task (see [the native handler lane](serving.md#the-native-handler-lane)); `false` keeps every handler in an asyncio task; refused under Granian |
| `server` | `tls_cert_file`, `tls_key_file` | unset | PEM certificate chain and private key; both or neither, and both must exist. Serves HTTPS on either server; the Rust server reads them once at start |
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
| `admin` | `secret` | unset | The server secret (32+ bytes, from the environment or `*_FILE`) that keys the admin's CSRF tokens; an admin without its own `secret=` needs it |
| `admin` | `session_hours` | `12` | `1` to `720`; a signed-in admin session ends this long after sign-in, whatever else happens |
| `admin` | `idle_minutes` | `120` | `1` to `session_hours * 60`; ... or this long after its last request |
| `admin` | `min_password_length` | `12` | `8` to `128`; the shortest password the admin's own users may have |
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

### Reverse proxies and request IDs

Forwarded metadata is ignored unless the immediate socket peer matches `server.trusted_proxies`. Configure only the addresses or CIDRs of proxies that remove client-supplied forwarding headers and write their own values. For example:

```python
class BlogSettings(settings.AppSettings):
    ...
    # The private address used by this deployment's ingress, not every private network.
    server: settings.ServerSettings = settings.ServerSettings(
        trusted_proxies=("10.20.0.0/16",),
        request_id_header="x-correlation-id",
        trust_request_id=True,
    )
```

Set `logging.include_request_id=true` to add the resolved ID to Hinoki's own log records while a request is being handled. Text logs append `request_id=<id>` and JSON logs include a `request_id` field. The ID matches the response's configured request ID header. Startup and other records outside a request omit the field. This setting is off by default, and it does not change application or third-party logger handlers.

### Your own loggers

Declare loggers, handlers and formatters the way Django's `LOGGING` does, with a standard [`logging.config.dictConfig`](https://docs.python.org/3/library/logging.config.html#logging-config-dictschema) mapping, and point `logging.config` at it:

```python
# myapp/logs.py
LOGGING = {
    "version": 1,
    "formatters": {"short": {"format": "%(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "short"}},
    "loggers": {"orders": {"handlers": ["console"], "level": "INFO"}},
}
```

```
BLOG_LOGGING__CONFIG=myapp.logs:LOGGING
```

The value may also name a function that returns the mapping. It is applied when each worker starts, right after Hinoki's own `hinoki` logger setup, with `disable_existing_loggers` off unless your mapping sets it. A mapping that configures the `hinoki` logger replaces Hinoki's handler on it. A target that cannot be imported or is not a mapping raises during startup. Kiso's access log is written by the Rust server; set `server.access_log_format = "logger"` to route it through the `hinoki.access` logger.

From a trusted peer, Hinoki accepts a single `X-Forwarded-Proto` value of `http` or `https` and walks a bounded `X-Forwarded-For` chain from the peer side to the first untrusted address. `Request.scheme` and `Request.client` use those validated values. Invalid, repeated or ambiguous values fall back to the socket scope. `X-Forwarded-Host` is never used: `Host`, `allowed_hosts` and generated URLs continue to use the original Host header.

On a listener bound to `::`, an IPv4 peer can appear as `::ffff:a.b.c.d`, which a plain IPv4 entry does not match (the proxy is then untrusted); see [behind a reverse proxy](serving.md#behind-a-reverse-proxy).

Request IDs are generated by default. With `trust_request_id=True`, one inbound configured header is accepted only from a trusted immediate peer and only when it is 1–128 visible ASCII characters without commas or controls. Otherwise Hinoki generates a new ID. The selected value appears in `Request.request_id`, built-in error JSON and the configured response header. The proxy must sanitize the incoming header before forwarding it; do not trust a broad network range merely because it is private.

## 2. Load them when the app is built

```python
from hinoki import Hinoki, settings


class BlogSettings(settings.AppSettings, prefix="BLOG_", dotenv=".env"):  # where to read
    database: settings.DatabaseSettings  # also opens the database for each worker

def create_app() -> Hinoki:
    app_settings = BlogSettings.load()
    return Hinoki(settings=app_settings)
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
  BLOG_DATABASE__MAX_SIZE: must be an integer
  BLOG_EXECUTION__SYNC_WORKERS: must be an integer
  BLOG_ADMN_THEME: unknown setting
```

## 5. Secrets

* Declare them as `settings.Secret` (or `settings.Secret | None`). A `settings.Secret` prints as `***` in `repr`, `str`, logs, error messages, `describe()` and the settings command; you get the real text only with `secret.reveal()`, at the one place that needs it (`JWTKey(..., settings.jwt_secret.reveal())`).
* Put secrets in the environment, a secret manager that exports variables, or **files**: with Docker or Kubernetes secrets set `BLOG_JWT_SECRET_FILE=/run/secrets/jwt`; the file's content (without the trailing newline) is the value. Only a regular file up to 64 KiB is read; setting both `BLOG_JWT_SECRET` and `BLOG_JWT_SECRET_FILE` is an error.
* A `.env` file is for development; never commit it. Pickling or serialising a settings object stores its secrets in plain text, so don't.
* A `validate()` method you add to a group shows its message in errors, so never put a field's value in it.

## 6. See what is in effect

These commands inspect the checked-in Blog settings from the `examples/`
project directory; with the repository checkout, use its locked environment:

```sh
cd examples
uv run --project .. hinoki settings blog.settings:BlogSettings --prefix BLOG_ --dotenv .env --toml settings.toml
```

```text
BLOG_ADMIN_THEME              .env         paper
BLOG_DATABASE__URL            environment  ***
BLOG_EXECUTION__SYNC_WORKERS  default      8
BLOG_JWT_SECRET               file         ***
```

The second column is where each value came from: `override`, `environment`, `.env`, `toml`, `file` or `default`. `--format json` prints the same for tools. `--example` prints a ready `.env.example` (every variable with its default and type; secrets and required values blank):

```sh
uv run --project .. hinoki settings blog.settings:BlogSettings --prefix BLOG_ --example > .env.example
```

## 7. Use them

`Hinoki(settings=app_settings)` derives the runtime limits, the docs URLs, logging setup and application timezone from the built-in settings (an explicit argument such as `title=` still wins). `app.now()` returns an aware `datetime` in the configured timezone; it defaults to UTC. The timezone is configured in settings, not as a `Hinoki(...)` argument. Your own fields are on `app.settings`, and modules reach them as `ctx.settings` in their lifespan.

The `server` group (server, host, port, workers and the rest) is applied by `hinoki serve`/`dev` ([Running an application](serving.md)). Hinoki does not connect to secret-manager services itself; use a provider that supplies environment values or secret files.

## Debug, allowed hosts and CORS

These three are off or permissive by default, so an application behaves the same until you set them. Set them from the environment like any other field:

```sh
BLOG_DEBUG=true
BLOG_ALLOWED_HOSTS=api.example.com,.example.org
BLOG_CORS__ORIGINS=https://app.example.com,http://localhost:5173
```

### `debug`

With `debug` on, the `500 internal_error` answer for an unhandled exception also carries a `debug` object:

```json
{
  "error": "internal_error",
  "request_id": "5f0c…",
  "debug": {
    "exception": "builtins.RuntimeError",
    "message": "the cache is cold",
    "traceback": ["Traceback (most recent call last):\n", "…"]
  }
}
```

Declared errors, validation errors and the other framework answers are unchanged, and the exception is logged either way. Exception messages can contain data you would not publish, so keep `debug` off in a deployment; each worker logs a warning at startup while it is on. `debug` changes nothing else: it does not enable documentation pages, relax authentication or skip the host check, and `hinoki dev` does not set it.

### `allowed_hosts`

The `Host` header of every request must match one entry:

| Entry | Matches |
| --- | --- |
| `*` (the default) | Any host; nothing is checked |
| `api.example.com` | That host, on any port |
| `.example.org` | `example.org` and every subdomain |
| `127.0.0.1`, `::1` | That address (`[::1]:8000` in the header) |

Matching ignores case, the port and a trailing dot. The header must be exactly one host with an optional port; a request for another host, with two `Host` headers or without a usable one, receives `400 disallowed_host` before routing, authentication and body reading; documentation routes are covered too. Entries are names: a scheme, port, path, non-ASCII text (write an international name in its ASCII IDNA form, `xn--...`) or a partial wildcard such as `*.example.com` fails at load, and `*` cannot be mixed with other entries. A dot entry is as wide as it reads: `.com` allows every `.com` host.

Set the list in a deployment that is reachable under more names than it should answer to. While `allowed_hosts` is `*` and `debug` is off, each worker logs a warning at startup (logger `hinoki.runtime`, shown with the default logging settings): every `Host` header is accepted, so links, redirects and cache keys built from it, such as a password-reset link, can name a host the client chose (Host header injection). Name the hosts the deployment serves to silence it, for example `allowed_hosts=("app.example.com",)`, or `(".example.com",)` for a domain and its subdomains (`BLOG_ALLOWED_HOSTS=app.example.com`). It is a warning, not an error, so the default keeps working in development; with `debug` on it is not logged, and the in-process test clients (`ASGIClient`, `SyncClient`) and the MCP `run_scenario` tool, which receive no network `Host` header, do not log it either.

The in-process test clients send no `Host` header, so a test of an application with a restrictive list passes one: `await client.get("/x", headers={"host": "api.example.com"})`.

### `cors`

CORS is off while `cors.origins` is empty: no header is added and `OPTIONS` requests receive `405`. With origins configured:

- A request whose `Origin` is listed receives `access-control-allow-origin` with that origin and `vary: origin`, on every answer including errors, so a browser client can read a `401` or `422` body. `credentials = true` adds `access-control-allow-credentials: true`; `expose_headers` is sent as `access-control-expose-headers`.
- A request from any other origin, or without an `Origin`, is served normally without the permission headers and still carries `vary: origin`, so a shared cache never hands one origin's answer to another; the browser refuses to hand the response to the page. CORS tells browsers what to allow. It is not authentication and does not stop a non-browser client.
- A preflight (`OPTIONS` with `Origin` and `Access-Control-Request-Method`) is answered with `204`, the configured `methods`, `headers` and `max_age`, without reaching a route or its access policy. A preflight from an origin that is not listed receives `403 cors_origin_denied`.

Origins are compared exactly, in lower case as browsers send them: `https://app.example.com` and `https://app.example.com:8443` are different origins, and a trailing slash or path is refused at load. Browsers leave a default port out of `Origin`, so list `https://app.example.com`, not `https://app.example.com:443`. The single entry `*` answers every origin with `access-control-allow-origin: *`; it cannot be combined with `credentials`, and browsers will not send cookies or HTTP authentication to it.

The policy applies to the whole application, including the ASGI and WSGI adapters; there is no per-route CORS option. An answer that already sets `access-control-allow-origin` itself (a `Response` with that header) is sent as it is, without the policy's headers.

## Browser security headers and HTTPS

Every HTTP response gets `X-Content-Type-Options: nosniff`, the configured `X-Frame-Options` and `Referrer-Policy`, including errors, OpenAPI/docs and admin responses. Defaults are `SAMEORIGIN` and `same-origin`, preserving the admin's existing referrer privacy default. These fields are owned by the app security policy: configured values replace same-named headers returned by a route.

The choices are enums rather than arbitrary strings, and environment/TOML values must match the listed wire values exactly. `FrameOptions` provides `deny` and `sameorigin`. `ReferrerPolicy` provides `no-referrer`, `no-referrer-when-downgrade`, `same-origin`, `origin`, `strict-origin`, `origin-when-cross-origin`, `strict-origin-when-cross-origin` and `unsafe-url`. `unsafe-url` can disclose the full referring URL to another origin; use it only when that is intended.

```python
from hinoki import settings

class BlogSettings(settings.AppSettings):
    security: settings.SecuritySettings = settings.SecuritySettings(
        redirect_https=True,
        hsts_seconds=31_536_000,
        hsts_include_subdomains=True,
        frame_options=settings.FrameOptions.DENY,
        referrer_policy=settings.ReferrerPolicy.STRICT_ORIGIN,
    )
```

For configuration loaded from the environment, set `BLOG_ALLOWED_HOSTS=api.example.com` and `BLOG_SECURITY__REDIRECT_HTTPS=true`. A wildcard host policy cannot be combined with the redirect: Hinoki requires an explicit host allowlist before constructing an absolute `Location`, which avoids redirecting an attacker supplied `Host` value. The redirect is `308`, preserves the method and raw path/query, and runs before route lookup, authentication and body reading. A configured CORS preflight is answered before redirecting so browser preflight requests remain usable.

HSTS defaults off. Hinoki emits it only when the request's effective scheme is HTTPS. Behind TLS termination, configure the exact proxy addresses in `server.trusted_proxies`; an untrusted `X-Forwarded-Proto` does not enable HSTS or suppress the redirect. `hsts_preload` also requires `hsts_include_subdomains` and a `max-age` of at least one year. Use HSTS only after confirming that HTTPS is available for every covered host: browsers retain the policy after the response.

## 8. Database connection ownership

For local development, `DatabaseSettings.url` defaults to `sqlite://./.hinoki/local.sqlite3`. Hinoki creates the hidden directory and database file when the module starts; it does not connect at module import or when models are imported. Configure a server database with one URL or with `engine` and its connection fields (see [configure a database](databases.md#configure-the-application)). Environment, dotenv and constructor values can override the local default. The URL and the password are secrets and cannot be loaded from TOML; TOML can hold the other connection fields and the pool size.

A `database: DatabaseSettings` field is enough: Hinoki installs `DatabaseModule` for it (a module declared in `modules=` or installed in a factory replaces it). The module opens a pool in each worker's startup lifespan, fails startup if the database is unreachable, and closes that worker's pool on shutdown. Each worker owns its own pool, so budget `replicas × workers × max_size` against the server database's connection limit.

PostgreSQL URLs accept `sslmode=disable`, `prefer`, `require`, `verify-ca` or `verify-full`, and `sslrootcert=/path/to/ca.pem`. `prefer` tries TLS first and falls back to plaintext only if the server declines TLS; a certificate or handshake error fails closed. All TLS connections verify the server certificate chain and hostname. `require`, `verify-ca` and `verify-full` require TLS; Hinoki's `verify-ca` also verifies the hostname. Trust uses Mozilla WebPKI roots plus any PEM certificates in `sslrootcert` (maximum file size 1 MiB). `sslmode=require` therefore requires a trusted certificate too. `sslmode=allow` is unsupported. Keep the URL in a secret setting, and mount the CA file where each worker can read it.

SQLite accepts a file URL such as `sqlite:///srv/app/data.sqlite3`; relative paths are relative to the process working directory. In-memory databases are not supported because each pooled connection has an independent database. SQLite uses bundled SQLite 3.35 or newer, a bounded connection pool and a single-writer model. UUID and timestamp columns use text storage; JSON uses JSON text. SQLite `contains` matching is ASCII case-insensitive.

```text
postgresql://app:password@db.example/app?sslmode=require&sslrootcert=/run/secrets/db-ca.pem
```

```python
class BlogSettings(settings.AppSettings, prefix="BLOG_"):
    database: settings.DatabaseSettings

app = Hinoki(settings=BlogSettings, include=[health, Posts])
```

The configuration is automatic in the useful sense: `BLOG_DATABASE__URL` (or the connection fields) is loaded from settings and the database module connects as the server starts. Resource creation and cleanup remain explicit in the module lifecycle; there is no process-global connection opened during import. Factories remain useful when the app's routes or modules depend on runtime choices.

One-off scripts have a different lifecycle. `examples/blog/seed.py` opens a short-lived `Database` explicitly, applies migrations, inserts synthetic rows and closes the pool in `finally`. It runs outside the HTTP server, so there is no worker lifespan to own that resource. It is a one-time seed for a fresh disposable database, not a repeatable reset:

```sh
cd examples
BLOG_DATABASE__URL=postgres://... uv run --project .. python -m blog.seed
```

Use a disposable database for the example. The seed command is not a production migration mechanism and is not run at app startup.
