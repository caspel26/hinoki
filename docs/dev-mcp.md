---
status: implemented
related:
  - cli.md
  - ai-workflow.md
  - app-mcp.md
---

# Development MCP

`hinoki mcp TARGET` starts a newline-delimited JSON-RPC MCP server on stdio. It reads the registered application, enabled rules and optional model modules. It writes protocol messages to stdout and diagnostics to stderr. It does not write source code, rule configuration or waivers.

```sh
cd examples
uv run --project .. hinoki mcp tracker.app:create_app \
  --rules tracker.rules:tracker_rules \
  --package tracker \
  --models tracker.models
```

`TARGET` is an app or a no-argument factory. `--rules` selects a `RuleSet`; `--package` and `--models` can be repeated; `--waivers` selects the waiver file (default `hinoki-rules.toml`). Rule checks with side effects are not run by this server.

## Tools

The server returns each result as JSON in `structuredContent` and in a text content block. Results include `tool_version`. Tool errors set `isError` and include an `error` message.

| Tool | Input | Result |
| --- | --- | --- |
| `inspect_project` | `{}` | App title/version, registered actions (each with `id`, `method`, `path`, `status`, `access`, `atomic`, `pagination`, `parsers`, `renderers` and `location`), selected model metadata, and enabled rule count. Models are included only when supplied with `--models`. |
| `list_rules` | `{}` | Enabled rules and read-only waiver entries, including their file path, target, reason, owner and expiry. |
| `explain_action` | `{"action": "module.function"}` | Action signature and docstring, access and `atomic` declarations, generated OpenAPI operation, and diagnostics for the action. The action may also be named by class method or bare handler name when unique. |
| `explain_error` | `{"code": "validation_error"}` | Meaning and fix for a framework error code, HTTP error code or rule ID. |
| `propose_model` | `{"name": "Article", "fields": [...]}` | Suggested model, input/patch/output schemas and CRUD action source text. The returned text is not written to a file. Fields support `str`, `int`, `float`, `bool`, `uuid` and `datetime`, with optional `nullable`, `unique` and `max_length`. |
| `recent_requests` | `{}` or `{"limit": 20, "status_min": 400, "path_contains": "/items"}` | The newest requests the development server answered, oldest first, as the JSON access fields (`method`, `path`, `query`, `status`, `duration_ms`, `lane`). They come from a file the Kiso server writes when `server.recent_requests` is set (default read path `.hinoki/requests.jsonl`, or `hinoki mcp --requests PATH`); the file keeps about the last 1 to 2 MiB and without it the result is empty with a note. `limit` is 1 to 200. |
| `check_project` | `{}` or `{"phase": "static"}` | Enabled rule diagnostics with waivers applied, plus `ok` and `failing`. `phase` accepts `startup` or `static`. Effectful checks are excluded. |

`run_scenario` is omitted unless explicitly enabled with `--scenarios read` or `--scenarios write`. Its input requires `path`, a plain URL path beginning with `/`; optional fields are `method` (default `GET`), `query` (query string without `?`), string-valued `headers`, and `json`. The result contains HTTP `status`, response `headers`, bounded body text, `truncated`, and parsed JSON when available.

Scenarios run the real app lifespan and handlers in-process. `read` accepts GET, HEAD, OPTIONS and QUERY; a GET handler may still change data. `write` also accepts POST, PUT, PATCH and DELETE. Scenario requests have a 15-second limit and response bodies are capped at 20,000 bytes. The tool is marked as effectful. Enable it only under owner control and use a disposable database.

See [CLI options](cli.md#mcp) for startup configuration and [application MCP](app-mcp.md) for exposing selected routes as remote tools.
