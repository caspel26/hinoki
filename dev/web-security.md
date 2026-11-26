# Restrict hosts, origins and HTTPS

A new application answers any `Host`, sends no CORS headers, never redirects to
HTTPS and trusts no proxy. That keeps development friction-free; before a
deployment, name what it serves. The built-in groups (`cors`, `security`,
`server`) are set from the environment, `.env` or a TOML file the class names;
a value assigned to such a group in the class body is not read.

```python
# notes/settings.py
from hinoki import settings


class ProjectSettings(
    settings.AppSettings, prefix="NOTES_", dotenv=".env", toml="settings.toml"
):
    pass
```

```toml
# settings.toml
allowed_hosts = ["api.example.com"]

[cors]
origins = ["https://app.example.com"]

[security]
redirect_https = true
hsts_seconds = 31_536_000

[server]
trusted_proxies = ["10.20.0.0/16"]
```

Every field can also come from the environment, which is usually where a
deployment sets them and which wins over the TOML file:

```sh
NOTES_ALLOWED_HOSTS=api.example.com,.example.org
NOTES_CORS__ORIGINS=https://app.example.com,http://localhost:5173
NOTES_SECURITY__REDIRECT_HTTPS=true
```

## Debug, allowed hosts and CORS {#debug-allowed-hosts-and-cors}

These three are off or permissive by default, so an application behaves the same
until you set them.

### `debug` {#debug}

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

Declared errors, validation errors and the other framework answers are unchanged, and the exception is logged either way. Exception messages can contain data you would not publish, so keep `debug` off in a deployment; each worker logs a warning at startup while it is on. `debug` changes nothing else: it does not enable documentation pages, relax authentication or skip the host check (with an admin or a WebSocket route and `allowed_hosts` at `*` it narrows the hosts to loopback, see below), and `hinoki dev` does not set it.

### `allowed_hosts` {#allowed_hosts}

The `Host` header of every request must match one entry:

| Entry | Matches |
| --- | --- |
| `*` (the default) | Any host; nothing is checked |
| `api.example.com` | That host, on any port |
| `.example.org` | `example.org` and every subdomain |
| `127.0.0.1`, `::1` | That address (`[::1]:8000` in the header) |

Matching ignores case, the port and a trailing dot. The header must be exactly one host with an optional port; a request for another host, with two `Host` headers or without a usable one, receives `400 disallowed_host` before routing, authentication and body reading; documentation routes are covered too. Entries are names: a scheme, port, path, non-ASCII text (write an international name in its ASCII IDNA form, `xn--...`) or a partial wildcard such as `*.example.com` fails at load, and `*` cannot be mixed with other entries. A dot entry is as wide as it reads: `.com` allows every `.com` host.

Set the list in a deployment that is reachable under more names than it should answer to. While `allowed_hosts` is `*` (the default, and the case for an app built without settings) and `debug` is off, each worker logs a warning at startup (logger `hinoki.runtime`, shown with the default logging settings): every `Host` header is accepted, so links, redirects and cache keys built from it, such as a password-reset link, can name a host the client chose (Host header injection). Name the hosts the deployment serves to silence it, for example `allowed_hosts=("app.example.com",)`, or `(".example.com",)` for a domain and its subdomains (`NOTES_ALLOWED_HOSTS=app.example.com`). It is only a warning for a plain API, so the default keeps working in development. An app that includes the [admin](https://hinoki.caspel.dev/dev/admin.md) or registers a [WebSocket route](https://hinoki.caspel.dev/dev/websockets.md) is refused at startup instead (the lifespan fails with a message naming `allowed_hosts`, and `hinoki serve` logs the error and exits with status 1; the server may already have logged that it is listening, but it answers no request): the admin's CSRF check and the WebSocket same-origin check compare the `Origin` header with the `Host` header, so with `*` a DNS-rebinding page (`Host: evil.example`, `Origin: https://evil.example`) would pass both. Name the served hosts. `debug` lifts the refusal for local development only, and then the app accepts loopback hosts only: with `debug` on and `allowed_hosts` at `*`, an app that serves an admin or a WebSocket route accepts only the Host names `localhost`, `*.localhost`, any `127.0.0.0/8` address and `[::1]` (with or without a port, in any case, with or without a trailing dot) for the whole app, and any other `Host` receives `400`, so a DNS-rebinding page cannot reach a development server; set `allowed_hosts` to serve other names (an explicit list is always used as given). An app without an admin or a WebSocket route still accepts any `Host` under `debug`. Each worker logs a startup warning while the loopback-only rule applies. With `debug` on the any-host warning is not logged, and the in-process test clients (`ASGIClient`, `SyncClient`) and the MCP `run_scenario` tool, which receive no network `Host` header, do not log it either.

The in-process test clients send no `Host` header, so a test of an application with a restrictive list passes one: `await client.get("/x", headers={"host": "api.example.com"})`.

### `cors` {#cors}

CORS is off while `cors.origins` is empty: no header is added and `OPTIONS` requests receive `405`. With origins configured:

- A request whose `Origin` is listed receives `access-control-allow-origin` with that origin and `vary: origin`, on every answer including errors, so a browser client can read a `401` or `422` body. `credentials = true` adds `access-control-allow-credentials: true`; `expose_headers` is sent as `access-control-expose-headers`.
- A request from any other origin, or without an `Origin`, is served normally without the permission headers and still carries `vary: origin`, so a shared cache never hands one origin's answer to another; the browser refuses to hand the response to the page. CORS tells browsers what to allow. It is not authentication and does not stop a non-browser client.
- A preflight (`OPTIONS` with `Origin` and `Access-Control-Request-Method`) is answered with `204`, the configured `methods`, `headers` and `max_age`, without reaching a route or its access policy. A preflight from an origin that is not listed receives `403 cors_origin_denied`.

Origins are compared exactly, in lower case as browsers send them: `https://app.example.com` and `https://app.example.com:8443` are different origins, and a trailing slash or path is refused at load. Browsers leave a default port out of `Origin`, so list `https://app.example.com`, not `https://app.example.com:443`. The single entry `*` answers every origin with `access-control-allow-origin: *`; it cannot be combined with `credentials`, and browsers will not send cookies or HTTP authentication to it.

The application policy applies to every route that does not declare its own, including the ASGI and WSGI adapters (see [per-route CORS](https://hinoki.caspel.dev/dev/web-security.md#cors-per-route) below). An answer that already sets `access-control-allow-origin` itself (a `Response` with that header) is sent as it is, without the policy's headers.

### Per-route CORS {#cors-per-route}

An action can carry its own policy with `cors=http.Cors(...)`, for a public feed, a partner endpoint or a route that must not be readable from the origins the rest of the application allows. `http.Cors` takes the fields of `settings.CorsSettings` (`origins`, `methods`, `headers`, `expose_headers`, `credentials`, `max_age`) and requires at least one origin:

```python
# notes/apps/feed/api.py
from hinoki import Schema, http

from notes.security import reader  # an `Access` policy

partner = http.Cors(origins=("https://partner.example",), credentials=True)


class Feed(Schema):
    feed: str


@http.get("/feed/partner", access=reader, cors=partner)
async def partner_feed() -> Feed:
    return Feed(feed="partner")
```

- **Precedence.** The route's policy replaces the application's for that route; nothing is merged, so the application's origins do not apply to it. An action without `cors=` keeps the application policy, and a route with `cors=` works even while the application's `cors.origins` is empty.
- **Preflight.** A preflight is answered for the route the browser asks about: `Access-Control-Request-Method` names the method, the request path names the route. A route with its own policy answers with its own `methods`, `headers`, `max_age` and origin check (`403 cors_origin_denied` for an origin it does not list). A method that no route has on that path is answered by the application policy. A preflight never runs the route's `access` policy, a transaction or the handler.
- **Answers.** Every answer from a matched route, errors from `access` or validation included, carries that route's headers and `vary: origin`. Answers before a route is known (`404`, `405`) carry the application's.
- **Groups and generated CRUD.** `RouteGroup("/feed", cors=http.Cors(...))` is the default of its actions (`cors=None` on an action returns it to the application policy), and `crud(..., cors=http.Cors(...))` gives all generated actions the policy.

Kiso decides these natively; the ASGI and WSGI adapters apply the same rules in Python.

## Security headers and HTTPS {#browser-security-headers-and-https}

Every HTTP response gets `X-Content-Type-Options: nosniff`, the configured `X-Frame-Options` and `Referrer-Policy`, including errors, OpenAPI/docs and admin responses. Defaults are `SAMEORIGIN` and `same-origin`, preserving the admin's existing referrer privacy default. These fields are owned by the app security policy: configured values replace same-named headers returned by a route.

The choices are enums rather than arbitrary strings, and environment/TOML values must match the listed wire values exactly. `FrameOptions` provides `deny` and `sameorigin`. `ReferrerPolicy` provides `no-referrer`, `no-referrer-when-downgrade`, `same-origin`, `origin`, `strict-origin`, `origin-when-cross-origin`, `strict-origin-when-cross-origin` and `unsafe-url`. `unsafe-url` can disclose the full referring URL to another origin; use it only when that is intended.

```toml
# settings.toml
[security]
redirect_https = true
hsts_seconds = 31_536_000
hsts_include_subdomains = true
frame_options = "deny"
referrer_policy = "strict-origin"
```

For configuration loaded from the environment, set `NOTES_ALLOWED_HOSTS=api.example.com` and `NOTES_SECURITY__REDIRECT_HTTPS=true`. A wildcard host policy cannot be combined with the redirect: Hinoki requires an explicit host allowlist before constructing an absolute `Location`, which avoids redirecting an attacker supplied `Host` value. The redirect is `308`, preserves the method and raw path/query, and runs before route lookup, authentication and body reading. A configured CORS preflight is answered before redirecting so browser preflight requests remain usable.

HSTS defaults off. Hinoki emits it only when the request's effective scheme is HTTPS. Behind TLS termination, configure the exact proxy addresses in `server.trusted_proxies`; an untrusted `X-Forwarded-Proto` does not enable HSTS or suppress the redirect. `hsts_preload` also requires `hsts_include_subdomains` and a `max-age` of at least one year. Use HSTS only after confirming that HTTPS is available for every covered host: browsers retain the policy after the response.

## Reverse proxies and request IDs {#reverse-proxies-and-request-ids}

Forwarded metadata is ignored unless the immediate socket peer matches `server.trusted_proxies`. Configure only the addresses or CIDRs of proxies that remove client-supplied forwarding headers and write their own values. For example:

```toml
# settings.toml
# The private address used by this deployment's ingress, not every private network.
[server]
trusted_proxies = ["10.20.0.0/16"]
request_id_header = "x-correlation-id"
trust_request_id = true
```

From a trusted peer, Hinoki accepts a single `X-Forwarded-Proto` value of `http` or `https` and walks a bounded `X-Forwarded-For` chain from the peer side to the first untrusted address. `Request.scheme` and `Request.client` use those validated values. Invalid, repeated or ambiguous values fall back to the socket scope. `X-Forwarded-Host` is never used: `Host`, `allowed_hosts` and generated URLs continue to use the original Host header.

On a listener bound to `::`, an IPv4 peer can appear as `::ffff:a.b.c.d`, which a plain IPv4 entry does not match (the proxy is then untrusted); see [behind a reverse proxy](https://hinoki.caspel.dev/dev/kiso.md#behind-a-reverse-proxy).

Request IDs are generated by default. With `trust_request_id=True`, one inbound configured header is accepted only from a trusted immediate peer and only when it is 1–128 visible ASCII characters without commas or controls. Otherwise Hinoki generates a new ID. The selected value appears in `Request.request_id`, built-in error JSON and the configured response header. The proxy must sanitize the incoming header before forwarding it; do not trust a broad network range merely because it is private.

Set `logging.include_request_id=true` to add the resolved ID to Hinoki's own log records while a request is being handled. Text logs append `request_id=<id>` and JSON logs include a `request_id` field. The ID matches the response's configured request ID header. Startup and other records outside a request omit the field. This setting is off by default, and it does not change application or third-party logger handlers.

## Errors and edge cases

- An invalid `allowed_hosts` entry, a `*` mixed with names, or a CORS origin with a path fails when settings load, before any worker starts.
- A served app with the admin or a WebSocket route, `allowed_hosts` at `*` and `debug` off does not start; with `debug` on it answers `400` to every `Host` that is not loopback (`localhost`, `*.localhost`, `127.0.0.0/8`, `[::1]`). The in-process test clients are exempt.
- `redirect_https=True` with `allowed_hosts` left at `*` is refused at load: the redirect needs a host it can trust.
- A request for an unlisted host receives `400 disallowed_host`; a preflight from an unlisted origin receives `403 cors_origin_denied`.
- Tests with a restrictive host list pass the header: `await client.get("/health", headers={"host": "api.example.com"})`.

## See also

- [Settings](https://hinoki.caspel.dev/dev/settings.md): how values are loaded, named and inspected.
- [Kiso server](https://hinoki.caspel.dev/dev/kiso.md#behind-a-reverse-proxy): addresses behind a proxy on Kiso.
- [Errors](https://hinoki.caspel.dev/dev/errors.md): the JSON bodies of these answers.
