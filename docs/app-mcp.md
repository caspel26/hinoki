---
status: implemented
related:
  - ai-workflow.md
  - authentication.md
---

# Expose an application over MCP

`expose_as_mcp` turns selected HTTP actions into tools for an MCP client. It is opt-in and uses an explicit allowlist. A call is dispatched through the app's ASGI adapter, so the action's authentication, access policy, validation and transaction still apply.

```python
from hinoki.mcpapp import expose_as_mcp

mcp_routes = expose_as_mcp(
    app,
    tools=["examples.tracker.api.list_tasks"],
    access=agent_access,
)
app.include(*mcp_routes)
```

By default, only `GET` and `HEAD` actions are exposed. Set `allow_writes=True` only when write tools are intended and the configured access policy permits them. The allowlist must match included actions; a bad or empty selection fails during setup.

The tool name, description, path/query parameters and JSON body shape come from the action's OpenAPI operation. Path values are encoded as path segments, unknown arguments are rejected, and output is bounded. The MCP route cannot expose itself as a tool.

Use bearer credentials for a remote MCP client when possible. Cookie-based authentication with write tools needs particular care. The endpoint accepts JSON `POST` requests. Set `allowed_origins` to the exact browser origins when browser clients are expected; other cross-site browser requests are rejected. `access` is required. For an intentionally public endpoint, pass `access=None, public=True` explicitly.

This application endpoint is distinct from `hinoki mcp TARGET`, the read-only development MCP over stdio. It does not provide MCP resources, prompts, sessions or OAuth discovery.
