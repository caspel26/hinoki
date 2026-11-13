# Structuring a Hinoki application: pattern and best practices

Opinionated, with reasons. It describes how the framework's features fit together; each rule is something the framework either enforces, can check with an optional rule, or leaves to review (marked **enforced**, **rule** or **convention**). The reference blog (`examples/blog`) and the one-file notes example (`examples/notes`) show the parts; `examples/tracker` is the layered example: `api` → `operations` → `models`, row permission in the statements, the direction enforced by `Imports.forbid`, operations tested without HTTP.

## 1. Three layers, one direction

```
HTTP layer      API classes / functions   shapes, status codes, access policy, transaction=True
   │            (hinoki.routing, Access, Schema)
   ▼
Operations      plain async functions     one business operation each; no Request, no HTTP types
   │            (commands/queries)
   ▼
Persistence     Model / QuerySet          tables, relations, explicit loading
```

* **HTTP layer** turns a request into typed values (`Schema`, path/query parameters, `Caller`) and an operation's result into a response schema. It contains no business rules and no SQL.
* **Operations** hold the rules ("an editor may publish only their own draft"). They take plain arguments (`principal`, a `Schema`, ids) so they can be called from an action, a CLI, a test, an admin hook (`ModelAdmin.save`) or an MCP tool. **Why:** an action that reads cookies or headers cannot be reused; an operation can.
* **Persistence** never imports the HTTP layer. **Rule:** `Imports.forbid(source="shop.models", target="shop.api")` (and the same for operations → api).
* Small apps may skip the middle layer (the notes example does). Add it when an action contains a rule you would want to call from somewhere else.

## 2. Where each concern lives

| Concern | Put it | Why |
| --- | --- | --- |
| Authentication | An auth provider (`BearerAuth`, `JWTAuth`, …) returning a `Caller` | One path, identical for HTTP, admin and MCP |
| Coarse permission | `Access(auth=…, permissions=[…])` on the action (or once for a group: `tasks = Endpoints("/tasks", access=member_only, atomic=True)`, then `@tasks.get("/")`). **Public is the default**: protect on purpose. **Rule:** `Actions.require_access` | A forgotten policy is the most common hole |
| Row permission (ownership, tenant) | In the **statement**: `queryset.filter(owner=…)` and `aupdate`/`adelete` scoped by it, never "read, check, write" | No race between check and use; hidden rows look like missing ones (404) |
| Input validation | The `Schema` (types, constraints, patterns) | Strict, no coercion, validated in Rust before your code runs |
| Business rules | Operations | Testable without HTTP |
| Transactions | `transaction=True` on the action (writes) | Commit after the response is encoded, rollback before the error becomes HTTP. **Rule:** `Transactions.require_atomic` |
| Errors | Raise `APIError` subclasses and **declare them on the action**: `@post(..., errors=(PostAlreadyPublished, Forbidden))`. Translate database errors once with `app.errors.handle(...)` | Failures are exceptions. The declaration (status and code come from the class) feeds OpenAPI, the MCP tool description and, in tests, `ASGIClient(app, strict_errors=True)`, which fails on an error answer the action did not declare. **A returned `Reply` commits the transaction** |
| Secrets | Settings/environment, passed to `JWTKey`, `Admin(secret=…)`, never defaults | Nothing to leak from the repository |

## 3. `Schema` classes and models

* Models describe tables; `Schema` classes describe the API. **Never return a model.** Name them by role: `PostIn`/`PostCreate` (what a client may set), `PostPatch` (every field `UNSET` by default), `PostOut` (what a client may see). **Rules:** `Actions.forbid_model_responses()` refuses an action whose return type is a `Model` (also inside `list`/`Page`/unions); `Schemas.forbid_exposure("password_hash", …)` keeps columns out of responses even through nesting and subclasses.
* Create from a `Schema` and add what only the server knows: `await Post.acreate(data, author_id=principal.user.id)` (server values win over the client's). Update with `await row.aupdate_from(patch)` (writes only what was sent). Respond with `PostOut.from_model(row, author_name=row.author.name)`.
* A server-assigned field must not exist in the input schema at all; the keyword override is a second line of defence.
* Constraints and patterns belong on the `Schema`, using the Rust pattern syntax (whole-value, no look-around).

## 4. Relations and queries

* Relations are never lazy. Load what the response shows (`select_related` for single values, `prefetch_related` for collections) in the same place that builds the response, so the cost is visible in one function.
* Paginate every list (`pagination=…` + `PageParams`). Never return an unbounded list; never `aall()` a table to count or aggregate (use `acount()`, filters, or SQL).
* Filter and order only by names you choose; never pass client-supplied column names to `filter` or `order_by` (the admin only sorts by its displayed columns for this reason).

## 5. Sync and async

Prefer `async` handlers. A sync handler runs on a bounded thread pool and may use the sync model methods (`Model.get`, `create`, …) through the worker's event loop; it must not block for long, and the queue is bounded (`overloaded` → 503). CPU-heavy work (password hashing) goes through `PasswordHasher`, which has its own bounded pool and releases the GIL. Never call blocking code from an `async` handler.

## 6. Modules

An application is a set of modules with explicit dependencies and a lifespan (`DatabaseModule`, your own `Module`). Importing a module registers nothing and opens nothing. Resources are created in the worker's lifespan, once per worker; migrations are applied by a deploy step, never at startup, never from every worker.

## 7. Migrations

`hinoki migrations plan` writes reviewable SQL and a state file; destructive operations are flagged and need `--allow-destructive`; adding a NOT NULL column without a default is flagged. Review the SQL like code, apply with `migrations apply` against a disposable database first.

## 8. Testing

* Behaviour tests against a **disposable PostgreSQL** (`HINOKI_TEST_DATABASE_URL`); a green run without it proves nothing about the database layer.
* Drive the application through the ASGI harness (no sockets) for HTTP behaviour; test operations directly.
* When replacing an engine, keep the old one as an **oracle** and compare on random and hostile input (see `tests/schemadifftest.py`).
* Use independent oracles for security code (stdlib HMAC, another library) rather than self-consistency; inject the clock (`JWTAuth.now`) instead of sleeping.
* Look at rendered UI in real browsers (`tests/browser/verify.py`) before calling it done.

## 9. Security defaults to keep

HTTPS terminates somewhere you control (set `Admin(secure_cookies=True)` behind a proxy); `Access` on everything that is not public on purpose; CSRF on every cookie-authenticated form (the admin does it); strict CSP; passwords with Argon2id only; JWT keys with a fixed algorithm and a `kid`; no token, password or hash in logs or error bodies; uploads held within the 1 MiB body cap unless you add streaming; real personal data never in tests or fixtures.

## 10. Rules to turn on from day one

`hinoki new` generates a project with `HNK-ACC-001`, `HNK-TXN-001` and `HNK-SCH-001` enabled. Add `Imports.forbid` for the layer direction in section 1. Waivers live in `hinoki-rules.toml`, are decided by the owner, have a reason and a bounded target, and are never edited by tools or agents to make a check pass.

## 11. Anti-patterns

* Returning a `Model` or a dict built from one.
* "Read the row, check the owner, then update" in two statements.
* A business rule inside an action that another entry point also needs.
* `Reply(4xx)` inside a `transaction=True` action (it commits).
* Client-supplied column names reaching `filter`/`order_by`.
* Applying migrations at startup; creating a pool at import time.
* A broad waiver (`*`) or editing waivers to silence a rule.
* Catching `Exception` in an action to "keep going": let the framework translate it and roll back.

## 12. More than one service

Split a system only for a reason you can name (independent deploys, different scaling, a team boundary), because every split costs something the framework cannot hide:

* **No joins, no shared transaction.** A foreign key never crosses a service. Store the other service's id as a plain value, and treat what it returns as a snapshot. A write that must touch two services is a sequence of calls that can half-fail: make each step idempotent (a client-supplied key, or a natural unique constraint) and decide what a retry does before writing the code.
* **Each service owns its tables.** Other services call its API; they never read its database. Migrations belong to the owner and are applied by its deploy step ([section 7](#7-migrations)).
* **The caller is a `Caller` too.** Service-to-service calls authenticate like any client (a JWT with a service identity and narrow `permissions`, verified by `JWTAuth`); do not forward the end user's token unless the callee must act as that user.
* **Wrap every remote call in an operation** ([section 1](#1-three-layers-one-direction)) with a timeout and a mapped failure (a 502/503 you chose), never letting a client library's exception become the response.
* **Share contracts, not models.** Publish the response `Schema` classes (or the OpenAPI document) that other services depend on; keep `Model`s private.

Not covered yet: a runnable two-service example and a client helper.
