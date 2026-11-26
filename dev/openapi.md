# OpenAPI document

`Hinoki.openapi()` builds an OpenAPI 3.2 document from the routes registered with the app (3.1 on request, see [the document version](https://hinoki.caspel.dev/dev/openapi.md#the-document-version)). Paths, parameters, request bodies, response schemas, declared errors and supported authentication schemes come from the action declarations and type annotations.

Generate the document from the application without starting a server:

```sh
hinoki openapi --output openapi.json
hinoki openapi --check openapi.json
hinoki openapi --breaking-against openapi-baseline.json
```

`--output` writes stable, sorted JSON. `--check` compares parsed JSON with a committed file, so whitespace-only changes do not report drift. `--breaking-against` compares with an earlier document (the released one) and prints one `breaking: …` line per change that would break an existing client. The three options are exclusive. The command opens no database and works even when `openapi_url` is disabled. See [inspect an application](https://hinoki.caspel.dev/dev/inspect.md#openapi) for output and exit status.

## Serve the document and viewers

All three URLs are disabled by default. Set `openapi_url` to serve JSON; add `docs_url` for the bundled Swagger UI or `redoc_url` for ReDoc. A project created with `hinoki new` already sets the first two:

```python
# notes/main.py
from hinoki import Hinoki

from notes.settings import ProjectSettings

app = Hinoki(
    settings=ProjectSettings,
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
)
```

The corresponding `docs.openapi_url`, `docs.docs_url` and `docs.redoc_url` settings provide the same defaults. A viewer requires `openapi_url`. `llms_url` does not: see [describe the API for agents](https://hinoki.caspel.dev/dev/openapi.md#llms). `Hinoki(...)` arguments override settings; passing `None` disables a page. The packaged viewers (Swagger UI 5.35.1 and ReDoc 3.2.0, which read OpenAPI 3.2 and list `QUERY` operations) work without a CDN; ReDoc 3 is loaded as a JavaScript module with its usage reporting turned off.

The generated OpenAPI document, Swagger UI, ReDoc and their assets answer `HEAD` with the same status and representation headers as `GET`, without a response body. Each request still has its own request ID in the configured response header (`x-request-id` by default).

## Describe the API for agents (`llms.txt`) {#llms}

The same document can be rendered as an [llms.txt](https://llmstxt.org/) Markdown text for a coding agent or an LLM client: the title and version, then the operations grouped by their first tag, each with its method, path, operation ID, summary and a few facts (`deprecated`, the security scheme names, the request and success response components). Untagged operations are listed under `Operations`.

```sh
hinoki llms
hinoki llms --output llms.txt
hinoki llms --check llms.txt
```

The command opens no database and works when `openapi_url` is disabled. `--check` exits `1` when the committed file differs from the generated text. See [inspect an application](https://hinoki.caspel.dev/dev/inspect.md#llms) for output and exit status.

Serving it is opt-in. Set `llms_url`, or `docs.llms_url`, and the text is answered at that path; the default `None` serves nothing:

```python
# notes/main.py
from hinoki import Hinoki

app = Hinoki(llms_url="/llms.txt")
```

The `Hinoki(...)` argument overrides the setting and `None` disables the page. Unlike the viewers it does not need `openapi_url`: the text is rendered from `Hinoki.openapi()` once at startup. It answers `GET` and `HEAD` as `text/markdown; charset=utf-8` (the media type of Markdown, RFC 7763) with `Cache-Control: no-cache`, and it is not listed in the OpenAPI document. `llms_url` cannot equal `openapi_url`, `docs_url` or `redoc_url`, and a route on the same path stops startup. `hinoki new` does not enable it.

The page is public, like the OpenAPI document when that is served. It is built only from the document, so it leaves out what the document leaves out: `include_in_schema=False` actions and the admin's pages. Leaving an action out neither hides it nor authorizes anything. The text names security schemes, never credentials.

The renderer, [`hinoki.ai.llms.render_llms`](https://hinoki.caspel.dev/dev/reference/tools.md), is Python: it reads the dictionary that the Python OpenAPI builder produces and does no I/O. The finished page is handed to Kiso as a prebuilt static answer, like the document and the viewers. [Hinoki's own documentation for agents](https://hinoki.caspel.dev/dev/ai-workflow.md#docs-for-agents) is a separate set of files.

## The document version

The `openapi` field is `3.2.0` by default. `Hinoki(openapi_version="3.1")` or the `docs.openapi_version` setting (`"3.1"` or `"3.2"`) writes a `3.1.0` document instead; any other value raises `ValueError`. `hinoki openapi` and the served document follow the application's choice. The version changes one thing: where a `QUERY` action is written. Every other operation, schema and component is the same in both.

```python
from hinoki import Hinoki

app = Hinoki(openapi_version="3.1")  # keep a 3.1 document for a 3.1-only consumer
```

OpenAPI 3.2.0 was checked against the bundled and current readers; use `"3.1"` for a consumer that only reads 3.1. `@apidevtools/swagger-parser` 13.1.0, for one, rejects every 3.2.0 document, and `openapi-typescript` 7.13.0 reads a 3.2 document but leaves its `query` operations out.

## QUERY operations

An action declared with `@http.query` is written under the path item's `query` field in a 3.2 document (Path Item Object, [OpenAPI 3.2.0 section 4.9](https://spec.openapis.org/oas/v3.2.0.html#path-item-object)), with the same operation object as any other method: operation ID, parameters, request body, responses and security.

```json
{"paths": {"/search": {"get": {"operationId": "search_page"}, "query": {"operationId": "search"}}}}
```

With `openapi_version="3.1"` there is no such field, so the same operation object is written under the `x-query` extension instead and the document stays valid 3.1. A 3.1 document with a `query` field would be invalid, and a 3.2 document never carries both, because the two entries would repeat the `operationId`.

Readers of a 3.1 document that look only at the standard method fields do not see `x-query` operations. The application MCP, `explain_action` and `hinoki openapi --breaking-against` read both fields, so a baseline written as 3.1 compares correctly with a 3.2 document.

## What is generated

* A unique `operationId` is derived from the handler's qualified name. If names collide, Hinoki appends a numeric suffix.
* A paginated response's envelope component is named after its items (`Page_Book`, `Page_integer`, `Page_string`). Two different security schemes, page envelopes or a page envelope and a `Schema` (a class named `Page_Book` beside `Page[Book]`) that would share a name get a numeric suffix (`KeyAuth_2`, `Page_array_2`, `Page_Book_2`) instead of overwriting each other; two different `Schema` classes with the same name are still a registration error.
* The first line of the handler docstring becomes `summary`; the remaining lines become `description`.
* Path, query and pagination parameters come from the bound handler signature. A list with [`filters=`](https://hinoki.caspel.dev/dev/crud.md#filter-order-and-search-the-list) documents one typed query parameter per filter: `boolean`, `integer`, `number` or `string` schemas (with `format` for dates, instants, UUIDs and decimals, and an `enum` for a `Literal`), `name.from` and `name.to` for a `Range`, `name.null` for a nullable field, a comma-separated array (`style: form`, `explode: false`, `maxItems`) for `In`, `search` with its length limit, and `ordering` as an array whose `enum` is each declared key and its `-` form. The same parameters appear in 3.1 and 3.2 documents. Body fields and successful response fields come from `Schema` annotations. Parser and renderer media types supply the content types.
* Typed `UploadedFile` parameters generate a `multipart/form-data` request body with binary file properties. Repeated files use an array of binary items; optional single-file bindings are not required in the body or property list. A `Form[Schema]` body next to files is documented as one object holding the schema's fields and the file properties, with the schema's `additionalProperties` and both lists of required names. Text keys read dynamically through `request.POST` are not inferred or listed.
* A declared `errors=(...)` adds response entries for those `APIError` classes. Request binding and schema validation errors are documented as `422` where the action has request inputs.
* `access=...` adds a security requirement when its authentication provider supplies an OpenAPI scheme. Authentication responses are included in the operation.
* Admin HTML routes marked `hide_from_docs` are omitted.

A `dict[str, X]` field becomes `type: object` with `additionalProperties` (the value schema), `propertyNames` (the key schema) and `minProperties`/`maxProperties` from its declared entry limits. The framework bounds on client input (1000 entries, 256-character keys) are not part of the document because components describe responses too; see [dictionary fields](https://hinoki.caspel.dev/dev/schema-collections.md#dictionary-fields).

`date`, `time` and `timedelta` fields become `type: string` with `format: date`, `time` and `duration`. A `Decimal` field becomes `type: string`, `format: decimal` and a `pattern` for its text form, with `ge`/`le`/`gt`/`lt` as the string extensions `x-minimum`/`x-maximum`/`x-exclusiveMinimum`/`x-exclusiveMaximum` (JSON Schema's numeric keywords do not apply to a string, and a string keeps the exact value); examples and defaults are written as decimal text; `max_digits` and `decimal_places` are not written, nor is a `Decimal` field's `multiple_of`. An `int` field's `multiple_of` becomes `multipleOf`. IP address and network fields become `type: string` with `format: ipv4`, `ipv6`, `ipv4network` or `ipv6network`. A `Path` field becomes `type: string` with `format: path`, and a `Secret` field `type: string` with `format: password` and `writeOnly: true`. A `bytes` field becomes `type: string`, `format: byte` and `contentEncoding: base64`, and its decoded-size limits are not written either, since they count bytes rather than characters. See [dates, durations, decimals and bytes](https://hinoki.caspel.dev/dev/schema-types.md#dates-durations-decimals-and-bytes).

An `Enum` or `Literal` field becomes `type: string` or `type: integer` with an `enum` list of the values (the member values, never the names), inline in the property; a nullable one is wrapped in `anyOf` with `null`. See [enums and literals](https://hinoki.caspel.dev/dev/schema-types.md#enums-and-literals).

A `set[X]` or `frozenset[X]` field becomes an array with `uniqueItems: true`, `tuple[X, ...]` an array of `X`, and a fixed `tuple[X, Y]` an array with `prefixItems`, `items: false` and equal `minItems`/`maxItems`. See [sets and tuples](https://hinoki.caspel.dev/dev/schema-collections.md#sets-and-tuples).

A [many-to-many ids field](https://hinoki.caspel.dev/dev/schemas.md#many-to-many-ids-fields) such as `tag_ids` (declared with `ids_name="tag_ids"`) is an array of `string` items with `format: uuid` (or the target key's type), `uniqueItems: true` and `maxItems` from its declared limit, in request and response schemas alike.

A plain union such as `int | str` becomes `anyOf` of its variants, and `Any` or `object` an unconstrained schema (`{}`). See [plain unions and `Any`](https://hinoki.caspel.dev/dev/schema-collections.md#plain-unions-and-any).

Discriminated unions declared as `Schema` fields become OpenAPI `oneOf` schemas with a `discriminator` and explicit tag-to-component mapping. Variant tag fields appear as required string enums; the runtime uses the same tags to select exactly one branch. List items, nested fields and nullable unions retain that structure. For route input and response documentation, put the union inside a wrapper `Schema`; bare union return annotations are rejected instead of producing an incomplete schema. The return value is also checked against the annotation at runtime ([details](https://hinoki.caspel.dev/dev/endpoints.md#the-response-is-checked-against-the-annotation)).

```yaml
oneOf:
  - $ref: "#/components/schemas/Cat"
  - $ref: "#/components/schemas/Dog"
discriminator:
  propertyName: type
  mapping:
    cat: "#/components/schemas/Cat"
    dog: "#/components/schemas/Dog"
```

See [Collections and unions: discriminated unions](https://hinoki.caspel.dev/dev/schema-collections.md#discriminated-unions) for the Python declarations and validation behavior.

For file binding syntax, limits and the in-memory boundary, see [upload files](https://hinoki.caspel.dev/dev/endpoints.md#upload-files).

An action annotated `-> Stream` or `-> File` documents its `200` response as `application/octet-stream` with `{"type": "string", "format": "binary"}`, and `-> EventStream` as `text/event-stream` with `{"type": "string"}`. The media type a `Stream` sends is chosen by the handler at run time, so the document does not list it. An annotation such as `-> Stream | NoteOut` also documents the plain arm, under the media types of the route's renderers, next to the streamed one. See [stream a response](https://hinoki.caspel.dev/dev/streaming.md).

Add a useful docstring to each handler because it is the source for operation text:

```python
@http.get("/tasks/{task_id}")
async def get_task(task_id: UUID) -> TaskOutput:
    """Read one task.

    Returns the task visible to the authenticated caller.
    """
    ...
```

## Per-action controls

`include_in_schema`, `tags`, `deprecated` and `operation_id` are options of `@http.get`, `post`, `put`, `patch`, `delete` and `query`:

```python
from hinoki import Access, Hinoki, Schema, http

from notes.security import staff_auth  # any authentication provider

admin_only = Access(auth=staff_auth, permissions=["ops:run"])


class Started(Schema):
    status: str


class Totals(Schema):
    tasks: int


@http.get("/ops/reindex", access=admin_only, include_in_schema=False)
async def reindex() -> Started:
    return Started(status="started")


@http.get("/v1/totals", tags=["tasks"], deprecated=True, operation_id="totalsV1")
async def totals() -> Totals:
    return Totals(tasks=0)


app = Hinoki(include=[reindex, totals])
```

* `include_in_schema=False` leaves the action out of `Hinoki.openapi()`, `hinoki openapi` and `/openapi.json`. The action is still registered, served and protected by its `Access` exactly as before: leaving it out neither hides it from a caller who knows the path nor authorizes anything. A path whose actions are all left out is not listed, and a `Schema` or security scheme used only by left-out actions does not appear under `components`.
* `tags` lists non-empty names, in order and without repeats, written to the operation's `tags`.
* `deprecated=True` writes `deprecated: true`. A deprecated action also tells its callers at run time: each response carries a `Deprecation` header and the call is logged as a warning, see [deprecate an action](https://hinoki.caspel.dev/dev/openapi.md#deprecate-an-action).
* `operation_id` replaces the generated operation ID. It must be a non-empty string, and two documented actions with the same value stop `include` with a `RegistrationError`; a generated ID never takes a value that an action declared. A left-out action does not take part in that check.
* `RouteGroup(prefix, include_in_schema=..., tags=...)` sets defaults for its actions: an action's own `include_in_schema` wins, and its `tags` follow the group's.
* `crud(...)` takes `include_in_schema` as one flag for the whole resource or a mapping by action (`{"delete": False}`; unknown names are refused), plus `tags` and `deprecated` for every generated action. It has no `operation_id`, because one value cannot name several actions.
* An MCP tool is built from the OpenAPI document, so `expose_as_mcp` refuses an action left out with `include_in_schema=False` instead of exposing it. The admin's HTML pages are always left out of the document and are not affected by these options.

## Deprecate an action

```python
from datetime import date

from hinoki import Schema, http


class Totals(Schema):
    tasks: int


@http.get("/v1/totals", deprecated=date(2026, 11, 22))
async def totals_v1() -> Totals:
    return Totals(tasks=0)
```

`deprecated` takes `True`, `False` or the `datetime.date` the action was deprecated on (a `datetime` is refused). It does three things:

* The document marks the operation `deprecated: true`.
* Every response, success or error, carries a `Deprecation` header: `@<unix seconds>` for a date (the form of RFC 9745, midnight UTC), `true` when no date is given.
* The `hinoki.deprecated` logger warns `deprecated action called: GET /v1/totals (N call(s) since the last report)`. An action is reported at most once per minute, with the calls it stayed quiet about, so a busy action does not flood the log; the state is one entry per deprecated action. Nothing is logged at startup.

The header and the warning are produced by the Python pipeline, so a deprecated action leaves [the native lane](https://hinoki.caspel.dev/dev/kiso.md#what-is-native-and-what-still-runs-in-python) (`hinoki routes --lanes` shows `lane_reason` `deprecated`) and, for generated CRUD, the native CRUD plan. The response and the access check do not change. There is no `Sunset` header.

## Current limits

`operation_id` is per action only, and `deprecated` is per action or set for every action generated by one `crud(...)`; a `RouteGroup` sets `tags` and `include_in_schema` but has no group-wide `deprecated` or `operation_id`. There is no tag metadata (descriptions) in the document's top-level `tags`. Response bodies are generated from annotations; every action's return value is checked against its annotation at runtime, and an annotation that cannot be checked stops the app at startup ([details](https://hinoki.caspel.dev/dev/endpoints.md#the-response-is-checked-against-the-annotation)). See [project status](https://hinoki.caspel.dev/dev/status.md) for other API boundaries.
