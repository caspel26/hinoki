# Request object, parsers and renderers — design (Q008)

Status: **S1 design, S2, S3 and S4 implemented** (2026-11-13): `Request` (Python), request-first binding, Rust query/cookie/header parsing and JSON decode/encode, parsers (JSON, form, multipart) with `parsers=`. S5–S7 not started. Owner request: a Django-style request, pluggable parsers and renderers, the request as the first argument of every handler, all of it Rust-based.

## Observed

* Handlers receive only bound values (`binding.py`): path/query scalars, one `Schema` body, `PageParams`, `Principal`. There is no request object; headers are visible only to auth providers (`Credentials`).
* Parsing is Python: `parse_query` (`urllib.parse`), `decode_body` (`json.loads`, JSON only, 415 otherwise), `header_map`. Responses are `json.dumps` in `responses.py`; the content type is fixed to JSON.
* Rust owns the router, the DB bridge and the SQL compiler (`crates/bindings`, `crates/orm`). `serde_json` is already a dependency.
* The confirmed syntax (`AGENTS.md`) fixes decorators and `access`/`transaction` options. It says nothing about a request parameter, so this is an **extension**, authorised by the owner on 2026-11-13, not a reintroduction of any forbidden form.

## Decisions

### D1 The request is the first handler argument

```python
@get("/health")
async def health(request: Request) -> dict: ...

class Posts(API, path="/posts"):
    @get("/{post_id}")
    async def retrieve(self, request: Request, post_id: int) -> PostOut: ...
```

* Function handlers: `request` first. Class handlers: `self`, then `request`. Registration fails if the first parameter is not annotated `Request` (clear message naming the action). No opt-out and no mixed mode: one shape everywhere, so docs, MCP and generated CRUD agree.
* Bound values (path, query, body schema, `PageParams`, principal) stay keyword parameters after it. `request.user` is the same principal that `access` produced; nothing authenticates twice.
* Breaking change for every existing handler, test and example. Done in one step (Q008-S2) with a mechanical rewrite, not a compatibility shim.

### D2 `Request` is Django-shaped and read-only

| Attribute | Meaning |
| --- | --- |
| `method`, `path`, `scheme`, `host`, `content_type` | request line, `content_type` without parameters |
| `headers` | case-insensitive mapping; repeated headers joined with `", "` (current rule) |
| `GET` | query parameters, `QueryDict`-like: `get`, `getlist`, last value wins on `[]` |
| `POST` | form fields (urlencoded/multipart), empty for JSON |
| `FILES` | uploaded parts (name, filename, content type, bytes, size) |
| `COOKIES` | parsed `Cookie` header |
| `body` | raw bytes, bounded by `MAX_BODY` |
| `data` | the parsed body (see D3); parsed lazily on first access, once |
| `user` | the principal, or `None` for public actions |
| `META`-style extras | `client`, `request_id`, `path_params` |

`data`, `POST` and `FILES` parse on first use only. A handler that declares a `Schema` body parameter still gets validation; `request.data` is the same parsed object. Mutating the request is not supported (frozen), because a shared mutable request context is forbidden by `AGENTS.md`.

### D3 Parsers

* A parser is selected by the request `Content-Type`, in priority order per action or application: `parsers=[JSONParser, FormParser, MultiPartParser]`. Default: JSON only, so behaviour stays what it is today (415 `unsupported_media_type` for anything else).
* Built-ins, all in Rust: `JSONParser` (serde_json, `arbitrary_precision`, rejects `NaN`/`Infinity` like today), `FormParser` (`application/x-www-form-urlencoded`), `MultiPartParser` (streaming-bounded: part count, per-part and total size limits).
* A custom parser is a Python class with `media_type` and `parse(raw: bytes, content_type: str) -> object`; it runs after the size check and may raise `InvalidBody`. Documented as the slow path (crosses into Python).
* Limits are settings with hard defaults: body size, JSON depth, multipart parts and file size. Violations answer 413/400, never truncate.

### D4 Renderers

* The handler return value is rendered by a renderer chosen from `Accept` among the action's `renderers=[...]` (default `JSONRenderer`). No match: 406 `not_acceptable`. Errors use the same negotiation and fall back to JSON.
* Built-ins: `JSONRenderer` (Rust, `serde_json`, same `allow_nan=False` rule), plus a documented extension point `render(value, request) -> bytes` with `media_type`. Docs/OpenAPI list the response media types per action.
* Returning an explicit `Response(...)` bypasses negotiation (existing behaviour, kept).

### D5 What "Rust-based" means here

Honest boundary (per `AGENTS.md`: do not call a path native if it crosses Python):

* **In Rust:** header and query decoding, cookie parsing, form and multipart parsing, JSON parse into Python objects (built directly with PyO3, no intermediate `json.loads`), JSON serialisation of the response, size and depth limits.
* **Still Python:** the ASGI callable (Granian drives Python), the `Request` object's Python class (a thin `#[pyclass]` over Rust-owned data is the target), handlers, schema validation, custom parsers/renderers.
* Recommendation: expose `Request` as a `#[pyclass]` that owns the raw ASGI data and parses on demand, so unused parts cost nothing. A full Rust HTTP server is out of scope and unaffected by this item.

No performance claim is made until the end-to-end benchmark (V10) exists; the first measurable is JSON parse + serialise against the current Python path, same payloads, ≥5 samples each, per `AGENTS.md`.

## Alternatives considered

* **Request optional** (injected when annotated): lower churn, but two handler shapes. Rejected: the owner asked for it first, and one shape is simpler to document and check.
* **Parse everything eagerly in Rust**: simpler ownership, but pays for form/multipart/cookies on every request. Rejected for lazy parsing.
* **Pydantic-style parser/renderer registries**: rejected, no Pydantic dependency; settings stay Hinoki's own.

## Failure, empty and private states

Empty body with a body schema → 422 as today; missing/unknown `Content-Type` → 415; body over limit → 413; malformed JSON/form/multipart → 400 with the parser's code; unacceptable `Accept` → 406; renderer raising → 500 `internal_error` (logged with `request_id`, never the body). `request.user` is `None` on public actions. Uploaded file bytes are never logged. Multipart temp storage is memory-bounded; no disk spooling in the first version.

## Tests

* Registration: missing/wrongly annotated `request` fails at class creation; works for functions, class methods, generated CRUD.
* Parsers: JSON edge cases (current suite ported), urlencoded repeats and decoding, multipart limits and malformed boundaries, unsupported media type, parser priority.
* Renderers: `Accept` matching, 406, error fallback to JSON, NaN rejection, custom renderer.
* Request: header case-insensitivity and joining, `GET.getlist`, cookies, lazy parsing happens once, `user` equals the principal.
* Typing fixtures (mypy strict): `Request` attributes, handler shape.
* Rust unit tests for each parser/limit; binding tests in Python against the wheel; OpenAPI shows request/response media types.
* Benchmark: parse+serialise Rust vs Python path, five baseline and five current runs.

## Steps

| Step | Deliverable | Files |
| --- | --- | --- |
| S1 | This design | `docs/REQUEST-DESIGN.md` |
| S2 (done) | `Request` (Python class, read-only, lazy) and request-first handler binding; rewrite handlers, tests, blog, CRUD templates | `python/hinoki/request.py`, `runtime/binding.py`, `runtime/requests.py`, `crud.py`, `examples/`, `tests/` |
| S3 (done) | Rust query/header/cookie parsing and JSON parse/serialise; swap Python implementations behind the same interface | `crates/bindings/src/request.rs`, `python/hinoki/runtime/` |
| S4 (done) | Parsers: form + multipart in Rust, `parsers=` option, limits as settings | `crates/bindings/src/parse.rs`, `routing.py`, `settings` |
| S5 | Renderers: `renderers=`, `Accept` negotiation, 406, OpenAPI media types | `runtime/responses.py`, `openapi.py` |
| S6 | `Request` as a Rust `#[pyclass]` if S3 measurements justify it; benchmark and report | `crates/bindings`, `benchmarks/` |
| S7 | Independent review (request handling touches authentication and untrusted input), fixes, docs, handoff | — |

## Open (owner)

Whether the default parser set should include form/multipart (recommended: JSON only by default, others opt-in); whether `request.user` should be the only way to reach the principal or the `Principal` parameter stays (recommended: both, identical object).

## Implementation notes (S2)

* `Request` lives in `python/hinoki/request.py`; the first parameter after `self` must be annotated `Request` and be positional-or-keyword, checked in `runtime/binding.py` at include time (`split_request`). The remaining parameters bind exactly as before.
* The body is now read for every POST/PUT/PATCH action, not only those with a `Schema` parameter, so `request.body` is available and the 413 limit always applies. `request.data` parses once (`decode_body`, JSON only until S4) and is shared with the schema binding.
* `COOKIES` uses a small Python parser (first value wins, quotes stripped); `POST`/`FILES` arrive with S4. `Headers` and `QueryDict` are read-only mappings.
* Generated CRUD templates take the request too; the unused-argument lint is relaxed for `examples/` and `crud.py` only, because the first parameter is mandatory and a handler need not read it.

## Implementation notes (S3)

* `crates/bindings/src/request.rs`: pure functions (`parse_query`, `parse_cookies`, `join_headers`, unit-tested) plus a recursive-descent JSON parser that builds Python objects directly and a JSON writer over plain Python values. Python calls them from `runtime/binding.py` (`decode_body`, `parse_query`), `runtime/requests.py` (`header_map`), `request.py` (cookies) and `runtime/responses.py` (encoding).
* The Rust JSON decoder is deliberately stricter than `json.loads` (BOM, lone surrogates and depth over 128 are errors); the encoder compacts output and writes UTF-8 instead of `\u` escapes. `benchmarks/requestbench.py` records the measured effect: modest for JSON, large for query strings. `Request` stays a Python class; the `#[pyclass]` step (S6) is only justified if end-to-end measurements show it matters.

## Implementation notes (S4)

* `python/hinoki/parsers.py`: `Parser` base, `JSONParser` (default), `FormParser`, `MultiPartParser(max_parts, max_part_bytes)`, `Parsed`, `UploadedFile`, `MultiDict`/`QueryDict`. The multipart splitter is Rust (`parse_multipart` in `request.rs`, RFC 7578, bounded parts and per-part size, 8 KiB header block, 70-byte boundary); fields must be UTF-8. The whole body stays capped by `MAX_BODY` (1 MiB) before any parser runs.
* `parsers=[...]` is an option on every method decorator (default JSON only); the request chooses the parser by bare media type, answers 415 `unsupported_media_type` listing what is accepted, 400 with the parser's code (`invalid_json`, `invalid_form`, `invalid_multipart`). Lists are checked at declaration (non-empty, distinct media types). OpenAPI lists one request media type per parser with the same schema (it does not model file parts).
* A `Schema` body parameter validates `request.data`: for forms and multipart that is each field's last value as text, so non-string fields fail strict validation (no coercion). `request.POST` / `request.FILES` are empty when the content type is not one of the action's parsers.
* Not done: streaming/spooling to disk (uploads are held in memory within the 1 MiB cap), RFC 5987 `filename*`, a configurable body limit, nested multipart.
