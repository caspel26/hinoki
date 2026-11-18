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

Generate the document from a project target without starting a server:

```sh
cd examples
uv run --project .. hinoki openapi notes.main:app --output openapi.json
uv run --project .. hinoki openapi notes.main:app --check openapi.json
```

The command does not open a database and works even when `openapi_url` is disabled. It writes stable, sorted JSON, and `--check` compares parsed JSON so whitespace-only changes do not report drift. See the [command-line guide](cli.md#openapi) for output and exit-status details.

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

A `dict[str, X]` field becomes `type: object` with `additionalProperties` (the value schema), `propertyNames` (the key schema, always with a `maxLength`) and `minProperties`/`maxProperties` from its entry limits, the default bound of 1000 entries included; see [dictionary fields](schemas.md#dictionary-fields).

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

See [Schemas: discriminated unions](schemas.md#discriminated-unions) for the Python declarations and validation behavior.

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
