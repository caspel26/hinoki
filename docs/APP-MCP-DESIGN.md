# Application MCP — design (Q010)

Status: **implemented first slice** (2026-11-13). Goal: let an AI agent use a running Hinoki application's own actions as MCP tools, with exactly the authentication, permissions, validation and transactions a normal client gets. The *development* MCP (`python -m hinoki mcp`) describes and checks a project; this one *serves* it.

## Decisions

* **Opt-in, allowlist only.** `expose_as_mcp(app, tools=[...patterns])` publishes only the actions whose qualified name matches a pattern (same `fnmatch` rule as the rules engine). No pattern, no tools; an empty list is a configuration error.
* **A route like any other:** it adds one `POST` action (default `/mcp`) with an `Access` policy you pass in; an unprotected MCP endpoint needs `access=None` spelled out (`public=True`). Streamable-HTTP style: one JSON-RPC message per POST, JSON response; notifications get 202. No sessions, no server-sent events.
* **Same identity, same path:** a tool call becomes an in-process request through the application's own ASGI adapter carrying the caller's `Authorization`, `Cookie` and API-key headers. The action's access policy, validation, rate/size limits, transactions and error mapping all apply unchanged; the MCP layer grants nothing.
* **Browser guard.** Only `application/json` POSTs are served (415 otherwise); a request with an `Origin` outside `allowed_origins`, or `Sec-Fetch-Site: cross-site`, is refused (403), which stops a web page from driving the tools with the user's cookie and covers the MCP spec's Origin/DNS-rebinding rule. Agents and CLI clients send no `Origin`. Cookie authentication with `allow_writes=True` remains a risky choice; use bearer credentials.
* **Reads by default.** Only `GET`/`HEAD` actions are exposed unless `allow_writes=True`; write tools are annotated `destructiveHint`/`readOnlyHint: false` so clients can ask the human.
* **Tool shape from OpenAPI:** the name is the operation id, the description is the docstring, the input schema merges path/query parameters (top level) and the JSON request body (under `body`), with `$defs` from the components. Arguments are checked against the declared parameter names before dispatch; path values are percent-encoded; unknown arguments are refused.
* **No recursion, bounded output:** an MCP endpoint is never exposed as a tool (by marker, so a second mount cannot list the first); the nested request carries the caller's `client` and `Host`, and the inner route's response headers do not leak out; responses are capped (100 KB) and flagged `truncated`; errors come back as `isError` with the HTTP status and body the normal client would see, never a stack trace.
* **Not included yet:** resources and prompts, per-tool rate limits, session or progress notifications, an `Mcp-Session-Id`, OAuth discovery for MCP clients, and output schemas.

## Verification

Tests through the in-process client: listing (allowlist only, write tools hidden by default), call with path/query/body, identity and 401/403 pass through, validation errors surface as `isError`, no self-exposure, encoded path values cannot escape their segment, argument and size limits; checked with the official `mcp` client over HTTP; independent review.

## Review (2026-11-13)

An independent review found no privilege bypass and fixed: Content-Type/Origin/Sec-Fetch-Site guard, forwarding of the client address and Host to the nested request, Latin-1 credential headers forwarded byte-exact, an allowlist matching no included action fails when `expose_as_mcp` is called (not on the first request), the inner route's response headers restored, endpoints excluded by marker. Documented: `tools/list` shows every exposed tool's name and schema to any caller who passes the endpoint's own policy (the inner policy still decides each call); the inner request has its own request id that is not reported back.
