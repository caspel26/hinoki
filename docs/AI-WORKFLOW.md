# Building a backend with an AI agent and Hinoki

What exists today (2026-11-13), what it guarantees, and what it does not.

## 1. Start a project an agent can work in

```sh
uv run python -m hinoki new shop --dir ~/projects     # lower-case letters and digits only
cd ~/projects/shop && uv sync && uv run pytest
```

The generated project contains a health route, **rules that are already on** (`HNK-ACC-001` actions name an access policy, `HNK-TXN-001` writes use a transaction, `HNK-SCH-001` fields such as `password_hash` never appear in a response schema), `AGENTS.md` (commands, conventions, and where the agent's authority ends), `CLAUDE.md` (a pointer to it), `.mcp.json` (starts the read-only development MCP) and `hinoki-rules.toml` (the owner's exceptions). The project's own tests pass and `python -m hinoki check` exits 0; the test suite of this repository generates a project and runs both on every change.

## 2. What the agent gets

* **`AGENTS.md`**: the same conventions this repository uses: request first, public unless protected on purpose, `transaction=True` for writes, schemas rather than models in responses, explicit relation loading, create from a schema with server values winning, migrations as reviewed SQL.
* **The development MCP** (`python -m hinoki mcp`, read only): `inspect_project` (the real registry), `list_rules`, `explain_action` (signature, schema, access, diagnostics for one action), `check_project` (rule diagnostics with stable IDs and locations) `explain_error` (what a validation or HTTP error code means and how to fix it) and `propose_model` (source text for a model, its In/Patch/Out schemas and CRUD actions from a field list; returned, never written). The tools never write code, rules or waivers.
* **Optional `run_scenario`**, off by default: `python -m hinoki mcp --scenarios read` lets the agent send GET/HEAD/OPTIONS requests to the app in-process and see status and body; `--scenarios write` allows any method. It runs the real handlers and the app's lifespan even in `read` mode (a GET handler may write), so it is not annotated read-only and should only face a disposable database. A failed startup is reported without its message (which may hold a connection string); the details go to the server's stderr. Only the owner can switch it on; the agent cannot.
* **Errors that say what to do**: validation errors list every problem with its path and a stable code; `explain_error` maps a code to its meaning and fix; a registration error names the action and parameter.

## 3. The loop

1. The agent asks the MCP for the project (`inspect_project`) instead of guessing from files.
2. It makes a small change (a model, a schema, an action).
3. It runs `pytest`, `ruff`, `mypy` and `python -m hinoki check`.
4. A rule violation is a stable ID, a location and an explanation. The fix is to change the code. **Waivers are the owner's decision**: `hinoki-rules.toml` is read by the tools, never written, and a waiver needs a reason, an owner and a bounded target. An agent that edits it to pass a check is violating `AGENTS.md`; the framework cannot stop someone with write access, it makes the change visible in review (put the file under CODEOWNERS).

## 4. Limits (what this does not do)

* No `run_scenario` tool yet (executing a request as a principal against a disposable database), no model/endpoint *proposal* tool, and no application MCP publishing actions to agents.
* The MCP has not been exercised by a real MCP client; it follows the protocol from the specification as known.
* Rules decide declarations, not what a function body does; static import rules read the project's own files and report dynamic imports as unverifiable.
* The guide was verified by generating a project and running its tests and `check`, not by watching an agent build a feature end to end.
