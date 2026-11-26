---
status: implemented
related:
  - function-endpoints.md
  - class-endpoints.md
  - errors.md
  - transactions.md
---

# Requests and responses

This page covers what one action receives and what it returns. How to organise actions is in [Function-based APIs](function-endpoints.md) and [Class-based APIs](class-endpoints.md); how they reach the application is in [Register routes](routes.md). The fragments below add actions to the `invoices` group of the Billing app, which supplies their access policy and transactions.

## Declare an action

```python
# notes/api.py
from hinoki import Schema, http


class Health(Schema):
    status: str


@http.get("/health")
def health() -> Health:
    return Health(status="ok")
```

Use `@http.get`, `@http.post`, `@http.put`, `@http.patch`, `@http.delete` or `@http.query` (`from hinoki import http`), or the same methods on a `RouteGroup`. Hinoki also answers `HEAD` through each `GET` route: it runs the GET response path and returns the same status and representation headers without a body, so the GET handler runs for HEAD requests too. A `405` response's `Allow` header includes `HEAD` whenever `GET` is available.

| Option | Meaning |
| --- | --- |
| `status` | Success status (default `200`) |
| `access` | `Access` policy; `None` is public |
| `atomic` | `True` runs the action in a transaction ([transactions](transactions.md)); there is no `transaction` option |
| `pagination` | Page-number policy ([pagination](pagination.md)) |
| `parsers`, `renderers` | Body formats in and out ([codecs](codecs.md)) |
| `errors` | `APIError` classes the action may raise ([errors](errors.md)) |
| `rate_limit` | A rate-limit policy ([below](#rate-limit-an-action)) |
| `cors` | `http.Cors(...)`: this action's own CORS policy, replacing the application's ([CORS](web-security.md#cors-per-route)) |
| `include_in_schema`, `tags`, `deprecated`, `operation_id` | What the [OpenAPI document](openapi.md#per-action-controls) says about the action; `include_in_schema=False` leaves it out without changing how it is served or protected, and `deprecated` (`True` or a `date`) also adds a `Deprecation` header and a warning log ([deprecate an action](openapi.md#deprecate-an-action)) |
| `validate_response` | `False` skips the [response check](#the-response-is-checked-against-the-annotation) |

Each option is checked when the route is declared or registered.

The fragments below continue `notes/apps/billing/api.py` from [function APIs](function-endpoints.md). Besides its imports they use these:

```python
from typing import Annotated

from hinoki import XML, Field, Form, MultiPartParser, Query, Request, UploadedFile
```

## Read with a body: `@http.query`

`QUERY` is the HTTP method for a read whose input does not fit a query string: it is safe and idempotent like `GET`, and carries a body like `POST`.

```python
# notes/apps/billing/api.py (continued)
class InvoiceSearch(Schema):
    text: Annotated[str, Field(min_length=1, max_length=40)]
    limit: Annotated[int, Field(ge=1, le=100)] = 20


@invoices.query("/search")
async def search_invoices(data: InvoiceSearch) -> list[InvoiceOut]:
    rows = await Invoice.filter(reference__contains=data.text).limit(data.limit)
    return InvoiceOut.from_models(rows)
```

```sh
curl -X QUERY http://127.0.0.1:8000/billing/invoices/search -H 'content-type: application/json' -d '{"text": "2026"}'
```

* The body is bound, parsed and validated exactly as for `POST`: the same parsers, size limit and `400`/`413`/`415`/`422` answers.
* A `QUERY` action must not change state. `RouteGroup(atomic=True)` applies to `POST`, `PUT`, `PATCH` and `DELETE` only; pass `atomic=True` on the action if the read needs a transaction.
* `@http.query` inside an `API` class works like the other methods. A `405` answer lists `QUERY` in `Allow` where it is available; `HEAD` is answered through `GET` routes only. Hinoki sends no `Accept-Query` or cache headers by itself.
* The method is recent. Granian passes it through over ASGI and WSGI (both are tested), but a proxy, gateway, firewall or HTTP client may reject it, and browsers send a CORS preflight for it. Keep a `GET` or `POST` route beside it when clients cannot send `QUERY`.
* In the OpenAPI document a `QUERY` operation appears under the path's `query` field (`x-query` with `openapi_version="3.1"`); see [OpenAPI](openapi.md#query-operations).

## Bind request values

Typed path parameters come from the path, query parameters from the query string, and a body parameter from the request body. Declare `request: Request` as the first parameter when you need headers, cookies or raw request data; it is injected only when that parameter is annotated.

```python
# notes/apps/billing/api.py (continued)
class InvoiceLabel(Schema):
    id: UUID
    locale: Annotated[str, Field(max_length=35)]


@invoices.get("/{invoice_id}/label")
async def invoice_label(request: Request, invoice_id: UUID) -> InvoiceLabel:
    locale = request.headers.get("accept-language", "en")[:35]
    return InvoiceLabel(id=invoice_id, locale=locale)
```

Request path, query and body values are validated before the handler runs; a `Schema` body uses `Schema.validate()` and reports nested field errors with its parameter prefix. Discriminated request variants belong in a wrapper `Schema` field (`Annotated[Cat | Dog, Field(discriminator="kind")]`); bare union body parameters are not supported. Hinoki does not coerce input values into different types, except in `Query[...]`, [`Form[...]`](#form-bodies) and [`XML[...]`](#xml-bodies).

Return a `Schema` built from the row: `InvoiceOut.from_model(row)` or `InvoiceOut.from_models(rows)` ([model schemas](model-schemas.md)). A returned `dict` where a `Schema` is declared is a `500 internal_error` ([the response check](#the-response-is-checked-against-the-annotation)). Use a wrapper `Schema` for a discriminated response union too; bare union return annotations are rejected by OpenAPI generation.

## Query strings as a Schema

`Query[...]` reads a whole query string into a `Schema`, for any method:

```python
# notes/apps/billing/api.py (continued)
class InvoiceFilter(Schema):
    paid: bool = False
    limit: Annotated[int, Field(ge=1, le=100)] = 20


@invoices.get("/recent")
async def recent_invoices(filters: Query[InvoiceFilter]) -> InvoiceRecent:
    rows = await Invoice.filter(paid=filters.paid).limit(filters.limit)
    return InvoiceRecent(items=InvoiceOut.from_models(rows))  # ?paid=true&limit=5
```

Text is converted to the `int`, `float` and `bool` the `Schema` declares before the strict validation (a repeated key fills a `list` field, a single one still does). A value that does not convert, a repeated key for a scalar, a missing required field and an unknown key (by default) are `422` errors, reported with the parameter name as prefix. The `Schema` owns the whole query string, so the action takes no other query or page parameters, at most one `Query[...]`, and it cannot be a path parameter. OpenAPI lists one query parameter per field.

## Form bodies

A plain `Schema` parameter is a strict JSON body. Wrap it in `Form[...]` to read the same `Schema` from a form, whose values are all text:

```python
# notes/apps/billing/api.py (continued)
@invoices.post("/import", status=201)
async def import_invoice(data: Form[InvoiceIn]) -> InvoiceOut:
    return InvoiceOut.from_model(await Invoice.acreate(data))  # reference=INV-1
```

Before validation, text is converted to the `int`, `float` and `bool` the `Schema` declares (and a lone value of a `list` field becomes a one-item list); `Schema` and JSON stay strict. A value that does not convert, or several values for a scalar, fails with the normal `422` type error. Empty text is not turned into `None`, and nested `Schema` fields are not read from form keys. An action with a `Form[...]` parameter and no `parsers=` uses `FormParser` (add `MultiPartParser()` explicitly for multipart text fields). `Form[...]` is valid only for the `Schema` body of a POST, PUT, PATCH or QUERY action, takes only form parsers (a JSON body needs a plain `Schema` parameter, so one cannot be coerced by mistake), and appears in OpenAPI as `application/x-www-form-urlencoded`.

## XML bodies

`XML[...]` reads a `Schema` from an `application/xml` body: the elements under the root become the fields, text is converted to the declared `int`, `float` and `bool`, and the strict validation follows.

```python
# notes/apps/billing/api.py (continued)
class Line(Schema):
    sku: Annotated[str, Field(max_length=40)]
    qty: int


class Order(Schema):
    customer: Annotated[str, Field(max_length=120)]
    lines: list[Line]


@invoices.post("/orders", status=201)
async def receive_order(data: XML[Order]) -> OrderReceipt:
    return OrderReceipt(lines=len(data.lines))
    # <order><customer>Ada</customer><lines><sku>a</sku><qty>2</qty></lines></order>
```

A repeated element is a list (a single one still fills a `list` field), nested elements fill nested `Schema` fields, and an empty element is `None`. Attributes are not read and namespace prefixes are dropped (`<a:x>` and `<b:x>` are both `x`). Malformed XML, a DTD, a custom entity, mixed text and child elements, a character XML 1.0 forbids (a control character other than tab, carriage return and line feed, written directly or as `&#1;`), an `encoding` declaration other than UTF-8, or exceeding `XMLParser(max_depth=..., max_elements=...)` is `400 invalid_xml`. `max_depth` defaults to 32 and may be raised to at most 1000 (`hinoki.parsers.MAX_XML_DEPTH`); a larger value raises `ValueError` when the parser is created. As with `Form[...]`, the action takes only `XMLParser` (selected when `parsers=` is omitted), and the OpenAPI document lists `application/xml`.

## Upload files

Bind a file by the multipart field name. An action that declares a file parameter and no `parsers=` gets `MultiPartParser()` automatically, with its default limits, unless the application's `parsers=` include one; pass `parsers=[MultiPartParser(...)]` to change them. `UploadedFile` contains the client filename, claimed content type and in-memory `bytes`; it does not save files or validate their content.

```python
# notes/apps/billing/api.py (continued)
@invoices.post(
    "/{invoice_id}/attachments",
    status=201,
    parsers=[MultiPartParser(max_parts=4, max_part_bytes=512_000)],
)
async def attach(
    request: Request, invoice_id: UUID, document: UploadedFile
) -> Attached:
    title = request.POST.get("title", "")[:120]
    # Validate content and choose a generated storage name before saving.
    return Attached(title=title, filename=document.filename, size=document.size)


@invoices.post("/{invoice_id}/scans", status=201)  # MultiPartParser() selected
async def attach_scans(invoice_id: UUID, scans: list[UploadedFile]) -> Attached: ...
```

`UploadedFile` requires exactly one part with that name. `list[UploadedFile]` requires at least one and keeps repeated parts in order. To make a single file optional, write `file: UploadedFile | None = None`. Declare the text fields next to the files as a `Form[Schema]` parameter; they are converted and validated like any form body (a failure is `422` with the parameter's path, such as `meta.pages`). A plain JSON `Schema` body cannot be combined with files and is refused at registration. Use `request.POST` for undeclared text fields or `request.FILES` when names or repetitions are dynamic.

```python
class ScanMeta(Schema):
    title: str
    pages: int


@invoices.post("/{invoice_id}/scan", status=201)
async def attach_scan(
    invoice_id: UUID, meta: Form[ScanMeta], scan: UploadedFile
) -> Attached:
    return Attached(title=meta.title, filename=scan.filename, size=scan.size)
```
 Parser precedence for a file action: its own `parsers=` (which must then include `MultiPartParser`, or registration fails), then an application-wide `Hinoki(parsers=[...])` that includes a `MultiPartParser`, then the automatic `MultiPartParser()`. An application parser list without multipart does not apply to file actions, and actions without file parameters keep their parsers.

All declared files require `multipart/form-data`. A missing file is a `422` validation error; a wrong media type is `415`; malformed multipart is `400`; a part or complete body over its configured limit is `413 payload_too_large`. A zero-byte body with no `Content-Type` is treated as absent input for optional files; an explicitly multipart empty body is malformed, while a valid closing-boundary-only body has no parts. The default complete-body limit is 1 MiB, and the parser buffers bytes in memory: this API is for bounded small uploads, not streaming.

!!! warning
    Concurrent uploads multiply memory use. Keep limits conservative, never use the supplied filename as a filesystem path, and verify content instead of trusting the declared media type. See [parsers and renderers](codecs.md#upload-files) and [testing uploads](testing.md#test-file-uploads).

## Responses

Return a value matching the declared response type, or return `Response` (`from hinoki import Response`) when the handler must set a status, headers or body that a typed result cannot express, annotated as `InvoiceOut | Response`. Declare expected `APIError` subclasses in `errors=(...)`; their status, stable code and message feed the OpenAPI document and the strict test client ([errors](errors.md)). Paginated actions are described in [pagination](pagination.md).

### Rate limit an action

`RateLimit(limit, window)` admits `limit` requests per `window` seconds for one key. Its counters live in a database table that every worker and host shares, so the limit holds across workers and after a restart. A route names it with `rate_limit=`:

```python
# notes/apps/accounts/api.py
from hinoki import RateLimit, http

from .schemas import LoginIn, SessionOut


@http.post("/auth/login", rate_limit=RateLimit(3, 3600, headers=True), atomic=True)
async def login(data: LoginIn) -> SessionOut: ...
```

The default storage, `DatabaseLimits()`, uses the `Database` of the worker and the `hinoki_rate_limits` table. Hinoki never creates it at import or at startup: register the app that owns it and apply its migration once per deployment.

```sh
hinoki app add ratelimit --models hinoki.ratelimit_store.models --migrations hinoki.ratelimit_store.migrations
hinoki migrations apply ratelimit
```

The algorithm is a cell rate (GCRA): a burst of `limit` is admitted at once, then one more request every `window / limit` seconds. A refused request uses up no quota. One statement counts each request, in the native core, on the async path and on the sync path alike. A route that otherwise runs on Kiso's native handler lane stays there.

| Argument | Meaning |
| --- | --- |
| `key` | `"client"` (default, the trusted client address; an IPv6 address counts by its /64), `"caller"` (the authenticated caller's id; an anonymous request falls back to the client) or a plain function of the `RateLimitContext` returning a string |
| `scope` | What the quota belongs to; the method and the route template (`GET /items`) by default, so one path under two methods has two quotas. Routes that give the same `scope` share one quota |
| `tenant` | A plain function of the context returning an extra key segment, so tenants never share a bucket |
| `cost` | What one request spends, at most `limit` |
| `unknown_factor` | A request with no known client address shares one bucket `unknown_factor` times as large (default 10). Set `server.trusted_proxies` behind a proxy |
| `headers` | `True` adds `RateLimit-Limit`, `RateLimit-Remaining` and `RateLimit-Reset` (whole seconds) to admitted and refused answers; the OpenAPI document lists them. Off by default |
| `on_error` | `"closed"` (default) answers `503 rate_limit_unavailable` when the table is unreachable or missing; `"open"` admits the request, counts it as `failed_open` and logs once a minute |
| `storage` | `DatabaseLimits(table=..., max_rows=..., timeout=..., sweep_every=...)` or `MemoryLimits(max_rows=..., sweep_every=...)`, which counts in one worker's memory and forgets on restart: for development and tests only. The buckets belong to the `MemoryLimits` instance, so routes given the same instance and the same `scope` and `tenant` count together |
| `clock` | A function returning an aware `datetime`; `hinoki.testing.ManualClock` in tests |

`window` is a whole number of seconds from 1 to 86,400. Arguments are checked when the route is declared: `limit` is a whole number from 1 to 4,294,967,295 and at most one request per microsecond of the `window`, and `timeout` is from 0.001 to 5 seconds; anything else is a `ValueError`. Keys are hashed in the native core and only the digest is stored: no address, user id or custom key reaches SQL, logs or metrics. The table holds at most `max_rows` buckets (default 1,000,000) and the migration indexes its `state` column; expired rows are deleted in small batches, oldest first, while requests are counted, and once the table is full of live rows every new key of a `scope` counts in one shared overflow bucket of that `scope`, `unknown_factor` times as large, until rows expire. Keys already in the table keep their own buckets; the overflow bucket adds at most one row per `scope`, and while it is in use the `tenant` segment does not separate new keys. A flood of new keys therefore spends the overflow bucket and is refused, never admitted without a count. A key part is limited to 1,024 bytes, and an oversized one is answered `503`. One check, the wait for a connection, the sweep of expired rows and the count included, is bounded by `timeout` seconds (default 0.5, at most 5); the sweep gets at most half of it and runs before the count, so a stale "full" mark never refuses the first new key after rows expire. Hosts stamp buckets with their own clock, so keep host clocks in sync. A refusal and the unavailable and failed-open outcomes are counted in the [metrics](metrics.md#what-is-exported); an `unavailable` outcome also counts a key, tenant or clock function that raised.

Any object with a `check` method is a policy too, when the application needs another store or rule:

```python
# notes/apps/accounts/api.py
from hinoki import RateLimitContext, errors, http

from .stores import RedisStore  # your module's resource


class LoginLimit:
    async def check(self, context: RateLimitContext) -> None:
        store = context.resource(RedisStore)  # provided by a module, per worker
        if not await store.allow(f"login:{context.client}", limit=5, window=60):
            raise errors.TooManyRequests(retry_after=60)


@http.post("/auth/login", rate_limit=LoginLimit(), atomic=True)
async def login(data: LoginIn) -> SessionOut: ...
```

`check` is `def` or `async def` and returns to allow the request. It runs after the route is matched and the caller authenticated, and before the body is read, parsed or handled, so a refused request costs neither a body read nor a password hash. A sync `check` runs on the bounded executor, so it must use a sync client with finite timeouts (cancelling the waiter does not stop its thread); an async one runs on the event loop.

`RateLimitContext` holds only `method`, `route` (the path template), `client` (the trusted client address, `None` when unknown; a forwarded address counts only from a configured trusted proxy), `caller` (the authenticated `Caller`, `None` on a public route) and `resource(kind)`, which returns the worker-owned resource that a [module](modules.md) provided, such as a Redis client or the `Database`. It carries no headers, credentials, body or query values.

The refusal is `errors.TooManyRequests(retry_after)`: status 429, code `rate_limited`, and `Retry-After` set to `retry_after`, a whole number of seconds from 1 to 86,400 (anything else is refused when the error is created). The route documents it in OpenAPI by itself, with `Retry-After` as a required integer header. Only that error answers 429. Any other exception from the policy or its store, including a `TooManyRequests` that was built without its constructor, is answered `503 rate_limit_unavailable`: the request is refused rather than allowed unchecked, the answer and the log name the route and never the exception, and the application's error handlers and debug output never see it. Cancellation is not caught.

Limiting failed credentials of a `BearerAuth` provider belongs in that provider, because authentication runs first.

A refusal emits a `RateLimited` [event](events.md#authentication-and-rate-limit-events) (policy class name, route template, `retry_after`; no key, caller or address) when the application has a handler for it. A handler cannot change the `429`.

### The response is checked against the annotation

Every action's return value is checked against its return annotation, and the annotation itself is checked when the app starts. There is nothing to switch on.

```python
# notes/apps/billing/api.py (continued)
@invoices.get("/{invoice_id}/raw")
async def raw_invoice(invoice_id: UUID) -> InvoiceOut:
    return {"id": invoice_id}  # a dict is not an InvoiceOut: 500 internal_error
```

- **At startup.** An annotation that cannot be checked stops the app with a `RegistrationError` that names the action: no annotation, `Any`, `object`, `set[...]`, a plain class, or `Response` inside a collection. Annotate a concrete type, or declare the action with `validate_response=False` to skip the check for that action.
- **At runtime.** A result that does not match is a `500 internal_error`, because the fault is the server's, not the client's. With the [`debug` setting](web-security.md#debug) on (development only; `hinoki dev` does not turn it on) the answer names the action, the type it returned and the annotation it broke, for example `raw_invoice returned a dict that does not match its return annotation InvoiceOut`. Without debug the body is generic. The log and the answer never contain the value itself. With `atomic=True` the check runs inside the transaction, so a mismatch rolls the write back.
- **What is accepted.** A `Schema` class, `list[...]`, `dict[str, ...]` and `Page[...]` of these, `str`, `int`, `float`, `bool` (a `bool` is not an `int`), `bytes`, `datetime`, `date`, `UUID`, `Decimal`, `None`, plain `dict` and `list`, `Any` or `object` as an item type, and unions of these. `Annotated[...]` is unwrapped. The check never coerces.
- **`Response`.** `-> Response` and `InvoiceOut | Response` are accepted: a returned `Response` carries its own status, headers and body and is not checked. The OpenAPI document describes `InvoiceOut | Response` by its typed arm, `InvoiceOut`. Actions with a bodyless status (`204`, `304`) are not checked.
- **What it does not check.** A `Schema` instance is valid by construction, so the check is about the shape of the result, not the field values. A custom renderer or a `dump()` override can still change the bytes afterwards.

## See also

- [Errors](errors.md)
- [Parsers and renderers](codecs.md)
- [Authentication](authentication.md)
