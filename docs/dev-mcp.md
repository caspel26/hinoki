---
status: implemented
related:
  - ai-workflow.md
  - app-mcp.md
  - cli.md
---

# Give an agent the development MCP

`hinoki mcp` starts an MCP server on stdio that describes your app, its rules and its models to a coding agent. It is read-only: it never writes source code, rules or waivers.

```sh
hinoki mcp notes.main:app --models notes.models
```

The rules default to the app's own `Hinoki(rules=...)` and the package to the target's package. Projects made by `hinoki new` contain a `.mcp.json` that starts this server for the agent. Protocol messages go to stdout as newline-delimited JSON-RPC; diagnostics go to stderr.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `TARGET` | required | The app, or a no-argument factory (`module:create_app`) |
| `--models MODULE` | none | Model modules to describe; repeatable |
| `--rules MODULE:NAME` | the app's rules | Another `RuleSet` |
| `--package NAME` | the target's package | Packages the rules inspect; repeatable |
| `--waivers PATH` | as for `hinoki check` ([which file](rules.md#which-waiver-file)) | The owner's waiver file, read only; must exist. `run_scenario` starts the app with it too |
| `--requests PATH` | `.hinoki/requests.jsonl` | Where `recent_requests` reads |
| `--scenarios off\|read\|write` | `off` | Registers `run_scenario` (see below) |

## Tools

Each result is JSON in `structuredContent` and in a text block, with a `tool_version`. A failed call sets `isError` and carries an `error` message.

| Tool | Input | Result |
| --- | --- | --- |
| `inspect_project` | `{}` | App title and version; each action's `id`, `method`, `path`, `status`, `access`, `atomic`, `pagination`, `parsers`, `renderers` and `location`; model metadata (with `--models`); the enabled rule count |
| `list_rules` | `{}` | Enabled rules and the waivers with their file, target, reason, owner and expiry |
| `explain_action` | `{"action": "module.function"}` | Signature, docstring, access and `atomic`, the OpenAPI operation and the action's diagnostics. A class method or a unique bare handler name also works |
| `explain_error` | `{"code": "validation_error"}` | Meaning and fix of a framework error code, HTTP error code or rule ID |
| `check_project` | `{}` or `{"phase": "static"}` | Rule diagnostics with waivers applied, plus `ok` and `failing`; `phase` is `startup` or `static`. Effectful checks are not run |
| `propose_model` | `{"name": "Article", "fields": [...]}`; optional `table`, `owner`, `path`, `unique_together` | Source text for a model, its input, patch and output `Schema` classes and CRUD actions. Returned, never written. Field types: `str`, `int`, `float`, `bool`, `uuid`, `datetime`, with `nullable`, `unique`, `max_length` |
| `recent_requests` | `{}` or `{"limit": 20, "status_min": 400, "path_contains": "/invoices"}` | The newest requests the development server answered (`method`, `path`, `query`, `status`, `duration_ms`, `lane`), oldest first; `limit` is 1 to 200 |

`recent_requests` reads the file Kiso writes when `server.recent_requests` is set; it keeps about the last 1 to 2 MiB, and without it the result is empty with a note. The `query` field is the raw query string and is not redacted: keep tokens out of query strings and treat the file as sensitive.

## Run requests against the app

`--scenarios read` or `--scenarios write` adds `run_scenario`. Its input needs `path` (a plain URL path starting with `/`); `method` (default `GET`), `query` (without `?`), string `headers` and `json` are optional. The result holds the HTTP `status`, response `headers`, the body text (at most 20,000 bytes, with `truncated`) and the parsed JSON when there is some.

| Mode | Methods |
| --- | --- |
| `read` | `GET`, `HEAD`, `OPTIONS`, `QUERY` |
| `write` | Also `POST`, `PUT`, `PATCH`, `DELETE` |

!!! warning "Scenarios run your real handlers"
    The app's lifespan and handlers run in-process, so even `read` can change data (a `GET` handler may write). Each request has a 15-second limit. Enable scenarios only yourself, never on an agent's request, and only against a disposable database.

## Errors and edge cases

* A failed app startup is reported to the client without its message, which may contain a connection string; the details go to stderr.
* Rule checks with side effects are never run by this server; use `hinoki check --allow-effects`.
* The server was exercised with the official `mcp` Python client (initialize, `tools/list`, `tools/call`); other clients are untested.

## See also

* [Build with a coding agent](ai-workflow.md)
* [Expose actions over MCP](app-mcp.md)
* [`hinoki mcp` options](cli.md#mcp)
