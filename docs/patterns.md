---
status: implemented
related:
  - function-endpoints.md
  - class-endpoints.md
  - rules.md
---

# Structure an application

Opinionated, with reasons. Each practice below is **enforced** by the framework, checkable with an optional **rule**, or a **convention** left to review. The [Tracker example](https://github.com/caspel26/hinoki/tree/main/examples/tracker) shows them together: `api` → `operations` → `models`, row permission in the statements, the direction enforced by `Imports.forbid`, operations tested without HTTP.

## 1. Three layers, one direction

```text
HTTP layer      actions (functions or API classes)   shapes, status codes, access, atomic=True
   │
   ▼
Operations      plain async functions                one business operation each; no HTTP types
   │
   ▼
Persistence     Model / QuerySet                     tables, relations, explicit loading
```

* **HTTP layer** turns a request into typed values (`Schema`, path and query parameters, `Caller`) and an operation's result into a response `Schema`. No business rules, no SQL.
* **Operations** hold the rules ("an editor may publish only their own draft"). They take plain arguments (`principal`, a `Schema`, IDs), so an action, a CLI command, a test, an admin hook or an MCP tool can call them. An action that reads cookies or headers cannot be reused; an operation can.
* **Persistence** never imports the HTTP layer. **Rule:** `Imports.forbid(source="notes.apps.billing.models", target="notes.apps.billing.api")`, and the same for operations → api.
* Small apps may skip the middle layer. Add it when an action holds a rule another entry point needs.

## 2. Where each concern lives

Actions are [functions](function-endpoints.md) by default, grouped in a `RouteGroup` that states shared options once; an [`API` class](class-endpoints.md) earns its place for `crud(...)` resources with hooks or inherited actions.

| Concern | Put it | Why |
| --- | --- | --- |
| Routes | Each app's `routes.py`; prefixes only in `hinoki.toml` ([register routes](routes.md)). **Convention** | One place to read the map; one source per path segment |
| Authentication | A provider (`BearerAuth`, `JWTAuth`, …) returning a `Caller` | One path for HTTP, admin and MCP |
| Coarse permission | `access=` on the action, or once on its `RouteGroup`. **Public is the default**: protect on purpose. **Rule:** `Actions.require_access` | A forgotten policy is the most common hole |
| Row permission (owner, tenant) | The **statement**: scope `aupdate`/`adelete` by owner so check and write are one operation | No check-then-write race; hidden rows look missing (`404`). If the API promises a distinct `403`, make the conditional write and its fallback explicit, and document what it reveals |
| Input validation | The `Schema` (types, constraints, patterns) | Strict, no coercion, validated in Rust before your code runs |
| Business rules | Operations | Testable without HTTP |
| Transactions | `atomic=True` on every write. **Rule:** `Transactions.require_atomic` | Commit after the response is encoded; rollback before the error becomes HTTP |
| Errors | Raise `APIError` subclasses and declare them: `errors=(InvoiceAlreadyPaid,)`. Translate exceptions you cannot change once with `@app.error_handler(...)` | The declaration feeds OpenAPI, MCP and `ASGIClient(app, strict_errors=True)`. A database `UniqueViolation` is already `409 unique_conflict`. **A returned `Response` commits the transaction** |
| Secrets | Settings and the environment, never defaults | Nothing to leak from the repository |

## 3. `Schema` classes and models

* Models describe tables; `Schema` classes describe the API. **Never return a model**: respond with `InvoiceOut.from_model(row)`. Name classes by role: `InvoiceIn` (what a client may set), `InvoicePatch` (every field `UNSET` by default), `InvoiceOut` (what a client may see, often `@derive(Invoice)`). **Rules:** `Actions.forbid_model_responses()` refuses a `Model` return type, also inside `list`, `Page` and unions; `Schemas.forbid_exposure("password_hash", …)` keeps columns out of responses through nesting and subclasses.
* Create from a `Schema` and add what only the server knows: `await Post.acreate(data, author_id=principal.user.id)`; server values win. Update with `await row.aupdate_from(patch)`, which writes only what was sent.
* A server-assigned field does not exist in the input `Schema` at all; the keyword override is a second line of defence.
* Constraints and patterns belong on the `Schema`, in the Rust pattern syntax (whole value, no look-around).

## 4. Relations and queries

* Relations are never lazy. Load what the response shows (`select_related` for single values, `prefetch_related` for collections) in the function that builds the response, so the cost is visible in one place.
* Paginate every list (`pagination=` + `PageParams` + `Page[...]`). Never return an unbounded list; never load a whole table to count or aggregate (use `acount()`, filters or SQL). **Rule:** `Actions.require_pagination`.
* Filter and order only by names you choose; never pass client-supplied column names to `filter` or `order_by`.

## 5. Sync and async

Prefer `async` handlers. A sync handler runs on a bounded pool and uses the sync model methods; it must not block for long, and a full queue answers `503 overloaded`. Password hashing has its own bounded pool. Never call blocking code from an `async` handler. See [sync and async code](sync-async.md).

## 6. Modules

An application is a set of modules with explicit dependencies and a lifespan (`DatabaseModule`, your own `Module`). Importing a module registers and opens nothing. Resources are created once per worker, in its lifespan. Migrations are a deploy step, never run at startup or from every worker. A standalone script owns and closes its own short-lived connection.

## 7. Migrations

```sh
hinoki migrations plan billing
hinoki migrations apply
```

`plan` writes reviewable SQL and a state file; destructive operations are flagged and need `--allow-destructive`, and a new `NOT NULL` column without a default is flagged. Review the SQL like code and apply it to a disposable database first. See [migrations](migrations.md).

## 8. Testing

* Run database tests against disposable servers for each backend you support: `HINOKI_TEST_DATABASE_URL` (PostgreSQL), `HINOKI_TEST_MYSQL_URL`, `HINOKI_TEST_MARIADB_URL`. Without a URL those tests are skipped: read the skip summary; one backend's green run is not conformance for another.
* Drive HTTP behaviour through the ASGI client (no sockets); test operations directly.
* For security code, use independent test vectors and inject the clock (`JWTAuth.now`).

## 9. Security defaults to keep

HTTPS terminates somewhere you control (`Admin(secure_cookies=True)` behind a proxy); `access=` on everything that is not public on purpose; CSRF on every cookie-authenticated form (the admin does it); a strict CSP; Argon2id passwords; JWT keys with a fixed algorithm and a `kid`; no token, password or hash in logs or error bodies; uploads within the 1 MiB body limit; no real personal data in tests or fixtures.

## 10. Rules to turn on from day one

`hinoki new` enables eight rules (`HNK-ACC-001`, `HNK-ACC-002`, `HNK-API-001`, `HNK-API-002`, `HNK-API-003`, `HNK-TXN-001`, `HNK-SCH-001`, `HNK-SCH-002`) and leaves the model rules `HNK-DB-001` and `HNK-DB-002` commented out until the first model exists. Turn those on then, and add `Imports.forbid` (`HNK-IMP-001`) for the direction in [section 1](#1-three-layers-one-direction). Waivers live in `hinoki-rules.toml`, are the owner's decision, carry a reason and a bounded target, and are never edited by tools or agents to pass a check.

## 11. Anti-patterns

* Returning a `Model`, or a dict built from one.
* A business rule inside an action that another entry point also needs.
* `Response(4xx)` from an `atomic=True` action: it commits.
* Client-supplied column names reaching `filter` or `order_by`.
* Applying migrations at startup; creating a pool at import.
* A broad waiver (`*`), or editing waivers to silence a rule.
* Catching `Exception` in an action to "keep going": let the framework translate it and roll back.
* `app.include(...)` in a project with `hinoki.toml`: list the routes in `routes.py`.

## 12. More than one service

Split a system only for a reason you can name (independent deploys, different scaling, a team boundary); every split costs something the framework cannot hide.

* **No joins, no shared transaction.** A foreign key never crosses a service. Store the other service's ID as a plain value and treat its answers as snapshots. A write across two services is a sequence of calls that can half-fail: make each step idempotent and decide what a retry does before writing the code.
* **Each service owns its tables.** Others call its API, never its database. Its migrations belong to it and are applied by its deploy step ([section 7](#7-migrations)).
* **The caller is a `Caller` too.** Service-to-service calls authenticate like any client (a JWT with a service identity and narrow permissions); do not forward the end user's token unless the callee must act as that user.
* **Wrap every remote call in an operation** ([section 1](#1-three-layers-one-direction)) with a timeout and a failure you chose (`502` or `503`), never a client library's exception.
* **Share contracts, not models.** Publish the response `Schema` classes or the OpenAPI document; keep models private.

A runnable two-service example and a client helper do not exist yet.
