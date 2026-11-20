---
status: implemented
related:
  - endpoints.md
  - schemas.md
  - settings.md
  - errors.md
---

# OpenAPI document

`Hinoki.openapi()` builds an OpenAPI 3.1 document from the routes registered with the app. Paths, parameters, request bodies, response schemas, declared errors and supported authentication schemes come from the action declarations and type annotations.

Generate the document from the application without starting a server:

```sh
hinoki openapi --output openapi.json
hinoki openapi --check openapi.json
hinoki openapi --breaking-against openapi-baseline.json
```

`--output` writes stable, sorted JSON. `--check` compares parsed JSON with a committed file, so whitespace-only changes do not report drift. `--breaking-against` compares with an earlier document (the released one) and prints one `breaking: …` line per change that would break an existing client. The three options are exclusive. The command opens no database and works even when `openapi_url` is disabled. See [inspect an application](inspect.md#openapi) for output and exit status.

## Serve the document and viewers

All three URLs are disabled by default. Set `openapi_url` to serve JSON; add `docs_url` for the bundled Swagger UI or `redoc_url` for ReDoc:

```python
from hinoki import Hinoki

app = Hinoki(
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
)
```

The corresponding `docs.openapi_url`, `docs.docs_url` and `docs.redoc_url` settings provide the same defaults. A viewer requires `openapi_url`. `Hinoki(...)` arguments override settings; passing `None` disables a page. The packaged viewers work without a CDN.

The generated OpenAPI document, Swagger UI, ReDoc and their assets answer `HEAD` with the same status and representation headers as `GET`, without a response body. Each request still has its own request ID in the configured response header (`x-request-id` by default).

## QUERY operations

The document is OpenAPI 3.1, which has no field for the `QUERY` method (OpenAPI 3.2 adds `query`). An action declared with `@http.query` is therefore written under the path item's `x-query` extension field, with the same operation object as any other method: operation ID, parameters, request body, responses and security. The document stays valid 3.1.

Tools that only read the standard method fields do not see these operations: Swagger UI and ReDoc do not list them, and a client generator skips them unless it is told about `x-query`. The application MCP and `explain_action` read the field.

## What is generated

* A unique `operationId` is derived from the handler's qualified name. If names collide, Hinoki appends a numeric suffix.
* The first line of the handler docstring becomes `summary`; the remaining lines become `description`.
* Path, query and pagination parameters come from the bound handler signature. Body fields and successful response fields come from `Schema` annotations. Parser and renderer media types supply the content types.
* Typed `UploadedFile` parameters generate a `multipart/form-data` request body with binary file properties. Repeated files use an array of binary items; optional single-file bindings are not required in the body or property list. Text keys read dynamically through `request.POST` are not inferred or listed.
* A declared `errors=(...)` adds response entries for those `APIError` classes. Request binding and schema validation errors are documented as `422` where the action has request inputs.
* `access=...` adds a security requirement when its authentication provider supplies an OpenAPI scheme. Authentication responses are included in the operation.
* Admin HTML routes marked `hide_from_docs` are omitted.

A `dict[str, X]` field becomes `type: object` with `additionalProperties` (the value schema), `propertyNames` (the key schema) and `minProperties`/`maxProperties` from its declared entry limits. The framework bounds on client input (1000 entries, 256-character keys) are not part of the document because components describe responses too; see [dictionary fields](schema-collections.md#dictionary-fields).

`date`, `time` and `timedelta` fields become `type: string` with `format: date`, `time` and `duration`. A `Decimal` field becomes `type: string`, `format: decimal` and a `pattern` for its text form, with `ge`/`le`/`gt`/`lt` as `minimum`/`maximum`/`exclusiveMinimum`/`exclusiveMaximum`; `max_digits` and `decimal_places` are not written, nor is a `Decimal` field's `multiple_of`. An `int` field's `multiple_of` becomes `multipleOf`. IP address and network fields become `type: string` with `format: ipv4`, `ipv6`, `ipv4network` or `ipv6network`. A `Path` field becomes `type: string` with `format: path`, and a `SecretStr` field `type: string` with `format: password` and `writeOnly: true`. A `bytes` field becomes `type: string`, `format: byte` and `contentEncoding: base64`, and its decoded-size limits are not written either, since they count bytes rather than characters. See [dates, durations, decimals and bytes](schema-types.md#dates-durations-decimals-and-bytes).

An `Enum` or `Literal` field becomes `type: string` or `type: integer` with an `enum` list of the values (the member values, never the names), inline in the property; a nullable one is wrapped in `anyOf` with `null`. See [enums and literals](schema-types.md#enums-and-literals).

A `set[X]` or `frozenset[X]` field becomes an array with `uniqueItems: true`, `tuple[X, ...]` an array of `X`, and a fixed `tuple[X, Y]` an array with `prefixItems`, `items: false` and equal `minItems`/`maxItems`. See [sets and tuples](schema-collections.md#sets-and-tuples).

A plain union such as `int | str` becomes `anyOf` of its variants, and `Any` or `object` an unconstrained schema (`{}`). See [plain unions and `Any`](schema-collections.md#plain-unions-and-any).

Discriminated unions declared as `Schema` fields become OpenAPI `oneOf` schemas with a `discriminator` and explicit tag-to-component mapping. Variant tag fields appear as required string enums; the runtime uses the same tags to select exactly one branch. List items, nested fields and nullable unions retain that structure. For route input and response documentation, put the union inside a wrapper `Schema`; bare union return annotations are rejected instead of producing an incomplete schema. As elsewhere, the response annotation does not cause runtime response validation.

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

See [Collections and unions: discriminated unions](schema-collections.md#discriminated-unions) for the Python declarations and validation behavior.

For file binding syntax, limits and the in-memory boundary, see [upload files](endpoints.md#upload-files).

Add a useful docstring to each handler because it is the source for operation text:

```python
@http.get("/tasks/{task_id}")
async def get_task(task_id: UUID) -> TaskOutput:
    """Read one task.

    Returns the task visible to the authenticated caller.
    """
    ...
```

## Current limits

Per-action `tags`, `deprecated`, a caller-supplied `operation_id` and `include_in_schema` controls are not implemented. Response bodies are generated from annotations; every action's return value is checked against its annotation at runtime, and an annotation that cannot be checked stops the app at startup ([details](endpoints.md#the-response-is-checked-against-the-annotation)). See [project status](status.md) for other API boundaries.
