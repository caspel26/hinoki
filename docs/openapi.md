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

## What is generated

* A unique `operationId` is derived from the handler's qualified name. If names collide, Hinoki appends a numeric suffix.
* The first line of the handler docstring becomes `summary`; the remaining lines become `description`.
* Path, query and pagination parameters come from the bound handler signature. Body fields and successful response fields come from `Schema` annotations. Parser and renderer media types supply the content types.
* A declared `errors=(...)` adds response entries for those `APIError` classes. Request binding and schema validation errors are documented as `422` where the action has request inputs.
* `access=...` adds a security requirement when its authentication provider supplies an OpenAPI scheme. Authentication responses are included in the operation.
* Admin HTML routes marked `hide_from_docs` are omitted.

Add a useful docstring to each handler because it is the source for operation text:

```python
@get("/tasks/{task_id}")
async def get_task(task_id: UUID) -> TaskOutput:
    """Read one task.

    Returns the task visible to the authenticated caller.
    """
    ...
```

## Current limits

Per-action `tags`, `deprecated`, a caller-supplied `operation_id` and `include_in_schema` controls are not implemented. Response bodies are generated from annotations; runtime response validation against those annotations is not implemented. See [project status](status.md) for other API boundaries.
