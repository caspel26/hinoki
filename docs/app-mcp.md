---
status: implemented
related:
  - dev-mcp.md
  - authentication.md
  - ai-workflow.md
---

# Expose actions over MCP

`expose_as_mcp` publishes selected HTTP actions as tools for a remote MCP client. Each tool call is dispatched through the app like an HTTP request, so the action's authentication, access policy, validation and transaction still apply.

```python
# notes/main.py
from hinoki import Hinoki
from hinoki.ai.mcpapp import expose_as_mcp

from notes.rules import project_rules
from notes.security import billing_staff
from notes.settings import ProjectSettings

app = Hinoki(
    settings=ProjectSettings,
    title="Notes",
    openapi_url="/openapi.json",
    docs_url="/docs",
    rules=project_rules,
)
mcp = expose_as_mcp(
    app,
    tools=["notes.apps.billing.api.list_invoices"],
    access=billing_staff,
)
app.include(*mcp)  # POST /mcp
```

`Hinoki(...)` has already included the routes listed in `hinoki.toml`; `expose_as_mcp` selects among those, so call it after the app exists.

## Choose the tools

`tools` are `fnmatch` patterns over qualified action names, the same names rules match: `module.function`, or `module.Class.method` for an action of an `API` class. `"notes.apps.billing.api.*"` selects every action of that module. Generated CRUD actions are named after the module that declares the `crud(...)` value (`notes.apps.billing.routes.InvoiceCrud.list`, see [Generated CRUD](crud.md)), so a module pattern selects them too.

* Only `GET`, `HEAD` and `QUERY` actions are exposed by default. A `QUERY` tool takes the request body as its input.
* `allow_writes=True` also exposes `POST`, `PUT`, `PATCH` and `DELETE` actions. Use it only when write tools are intended and `access` permits them.
* The tool name, description, path and query parameters and JSON body shape come from the action's OpenAPI operation. Unknown arguments are rejected and path values are encoded as path segments.
* The MCP route never exposes itself.

## Protect the endpoint

`access` is required. An intentionally public endpoint says so twice: `access=None, public=True`.

* Prefer bearer credentials for a remote client; cookie authentication with write tools needs particular care.
* The endpoint accepts JSON `POST` requests. Set `allowed_origins` to the exact browser origins when browser clients are expected; other cross-site browser requests are refused.
* Tool output is capped at 100,000 bytes (longer text is cut and marked `truncated`); a request message over 1,000,000 bytes answers `413`.

## Errors and edge cases

| When | What happens |
| --- | --- |
| `tools=[]` | `ExposureError` at the call |
| `access=None` without `public=True` | `ExposureError` |
| A pattern matches no included action, or the action is not in the OpenAPI document | `ExposureError` naming it |
| An action that returns `Stream`, `EventStream` or `File` matches `tools` | `ExposureError` naming it: a tool returns one complete result, so a [streamed response](streaming.md) cannot be exposed |
| `Transactions.require_atomic()` is enabled | The MCP endpoint is a `POST` without a transaction of its own; exclude it with `except_=["hinoki.ai.mcpapp.*"]`. Each tool call still runs the action's own transaction. |

The endpoint does not provide MCP resources, prompts, sessions or OAuth discovery.

## See also

* [Development MCP](dev-mcp.md): a local server that describes the project to coding agents
* [Authentication and access](authentication.md)
* [Rules](rules.md)
