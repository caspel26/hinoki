---
status: implemented
related:
  - ai-workflow.md
  - authentication.md
  - transactions.md
---

# Rules: architecture you can check

Rules are optional, typed promises about your application that a tool checks and that fail with a stable ID, a location and an explanation. They are not a linter for style and not an AI prompt: each rule is a Python object with deterministic behaviour. Nothing runs unless you enable rules.

Hinoki ships eleven built-in rules, each with a stable ID. The set is intentionally small: access declarations, atomic writes, response types, model responses, pagination, permissions, request text limits, table names, foreign-key indexes, schema exposure and import direction. Add a custom `Rule` only when a project has a concrete invariant that these checks cannot express.

## Enable them (the blog and the tracker enable all of them)

```python
# examples/blog/rules.py
from hinoki import Actions, Imports, Models, Schemas, Transactions
from hinoki.rules import RuleSet

blog_rules = RuleSet(
    Actions.require_access(match="blog.*", except_=PUBLIC),  # HNK-ACC-001
    Actions.require_permissions(match="blog.*", except_=PUBLIC),  # HNK-ACC-002
    Actions.forbid_model_responses(match="blog.*"),  # HNK-API-001
    Actions.require_return_type(match="blog.*"),  # HNK-API-002
    Actions.require_pagination(match="blog.*"),  # HNK-API-003
    Transactions.require_atomic(except_=["blog.accounts.*.login"]),  # HNK-TXN-001
    Schemas.forbid_exposure("token", "password_hash"),  # HNK-SCH-001
    Schemas.require_string_limits(),  # HNK-SCH-002
    Models.require_explicit_table(match="blog.*"),  # HNK-DB-001
    Models.require_foreign_key_index(match="blog.*"),  # HNK-DB-002
    Imports.forbid(source="blog.models", target="blog.api"),  # HNK-IMP-001
)

# examples/blog/main.py
app = Hinoki(..., rules=blog_rules)  # startup rules fail the app if a promise is broken
```

| ID | Rule | Phase | Checks |
| --- | --- | --- | --- |
| `HNK-ACC-001` | `Actions.require_access(match, except_)` | startup | every matching action names an `Access` policy (public by default is the common hole) |
| `HNK-ACC-002` | `Actions.require_permissions(match, except_)` | startup | an action that names an `Access` policy names at least one permission, so "any authenticated caller" is never implicit |
| `HNK-TXN-001` | `Transactions.require_atomic(methods, match, except_)` | startup | state-changing actions use `atomic=True` (the admin's own routes are not checked: they open their own short transactions, so that no connection waits while a password is hashed) |
| `HNK-API-001` | `Actions.forbid_model_responses(match, except_)` | startup | no action returns a `Model` (or a list/page of them): responses are `Schema` classes |
| `HNK-API-002` | `Actions.require_return_type(match, except_)` | startup | every matching action annotates a concrete return type (not missing, `Any` or `object`; `None` is fine for a 204). An action with a missing or unusable annotation already stops the app at startup unless it sets `validate_response=False`, so this rule matters for those actions |
| `HNK-API-003` | `Actions.require_pagination(match, except_)` | startup | a `GET` action never returns a plain `list[...]` (alone or in a union): it returns a `Page` |
| `HNK-SCH-001` | `Schemas.forbid_exposure(*fields)` | startup | named fields never appear in a response `Schema`, nested, in a subclass or in a variant of a discriminated union, as a field, an alias or a computed field; dict keys are runtime data and are not checked |
| `HNK-SCH-002` | `Schemas.require_string_limits()` | startup | every text field of a request `Schema` (nested ones and union variants included), and every text element of a list, set or tuple, or value of a `dict[str, ...]` field, and every text variant of a plain union, has `max_length` or `one_of` (an `Enum` or `Literal` field lists its values and passes); dict keys from clients are bounded by the validator |
| `HNK-DB-001` | `Models.require_explicit_table(match)` | startup | models whose module matches `match` pin their table with `@db.table(...)` or `table=`; a rename of the class then cannot change the physical table |
| `HNK-DB-002` | `Models.require_foreign_key_index(match)` | startup | every foreign-key column of a matching model leads an index, a primary key or a unique constraint |
| `HNK-IMP-001` | `Imports.forbid(source, target)` | static | modules under `source` never import `target` (read from the source files; dynamic imports are reported, not ignored) |

The model rules take `match` as an `fnmatch` pattern on the model's module (`blog.*`); a pattern that matches no model is reported as unverifiable. A project created by `hinoki new` enables the first eight rules (`HNK-ACC-001`, `HNK-ACC-002`, `HNK-API-001` to `HNK-API-003`, `HNK-TXN-001`, `HNK-SCH-001`, `HNK-SCH-002`) and carries the two model rules commented out until its first model exists; `Imports.forbid` is yours to add.

`match` and `except_` patterns on action rules use `fnmatch` on the action's full qualified name (`pkg.api.Posts.create`, named after the class it is mounted on). A bare name such as `health` matches nothing: write `*.health`.

## Run them

The Blog target is part of the checked-in project in `examples/`. Run this from
that directory, using the locked environment in the parent directory (the blog's admin needs `BLOG_ADMIN__SECRET`, see its README):

```sh
cd examples
export BLOG_ADMIN__SECRET='a-local-development-secret-with-32-bytes'
uv run --project .. hinoki check blog.main:create_app \
    --rules blog.rules:blog_rules --package blog
# 11 rule(s): 0 violated, 0 unverifiable, 0 waived       (exit 0)
```

A broken promise (a deliberately bad app from the tests):

```text
tests/projects/rulesapp.py:7: HNK-ACC-001 [violated] GET /health (health) declares no access policy
  fix: add access=<policy> to the action (or list it in except_ when it is public on purpose)
tests/projects/rulesapp.py:12: HNK-ACC-001 [violated] POST /items (create) declares no access policy
  fix: add access=<policy> to the action (or list it in except_ when it is public on purpose)
tests/projects/rulesapp.py:12: HNK-TXN-001 [violated] POST /items (create) does not declare atomic=True
  fix: add atomic=True to the action (or declare it on a RouteGroup(..., atomic=True) group)
2 rule(s): 3 violated, 0 unverifiable, 0 waived        (exit 1)
```

The other rules report the same way. This output comes from a small project whose `api.py` returns a `Model`, returns a plain `list` from a `GET`, exposes `token` in a response `Schema`, leaves a request text field unbounded, and whose `models.py` leaves a table name derived, a foreign key unindexed and imports `api`:

```text
shop/api.py:25: HNK-ACC-002 [violated] GET /members/{member_id} (one) names an Access policy without permissions
  fix: name at least one permission in the Access policy of the action
shop/api.py:25: HNK-API-001 [violated] GET /members/{member_id} (one) returns the model Member; return a Schema
  fix: return a Schema (for example PostOut.from_model(row)) instead of the Model
shop/api.py:35: HNK-API-002 [violated] POST /members (create) does not declare a concrete return type
  fix: annotate the action's return type (a Schema, or None for status 204)
shop/api.py:30: HNK-API-003 [violated] GET /members (listing) returns a plain list, not a Page
  fix: return Page[Item] and paginate the action instead of returning a list
shop/api.py:16: HNK-SCH-001 [violated] MemberOut.token is exposed by GET /members
  fix: remove the field from the response Schema, or return a Schema that does not carry it
shop/api.py:21: HNK-SCH-002 [violated] MemberIn.name is read from POST /members without a max_length
  fix: bound the field: Annotated[str, Field(max_length=...)] or one_of=...
shop/models.py:4: HNK-DB-001 [violated] Member uses the derived table name 'shop_members'
  fix: name the table with @db.table(...) or table=... on the model
shop/models.py:9: HNK-DB-002 [violated] Order.member_id references Member and has no index
  fix: index the column: field(index=True) or @db.indexed(...)
shop/models.py:14: HNK-IMP-001 [violated] shop.models imports shop.api, which is forbidden for shop.models
  fix: move the code to an allowed layer, or depend on it through the layer in between
```

(The output above is excerpted: `HNK-ACC-001`, `HNK-TXN-001` and the rest of the diagnostics are listed the same way.) When the return type does not name its schema, `HNK-SCH-001` reports `[unverifiable]` instead of passing silently.

`--format json` prints the same diagnostics as versioned data (`schema_version`, `rules`, `summary`, and per diagnostic `rule`, `severity`, `phase`, `status`, `message`, `location`, `explanation`, `subject`, `waiver`, `fix`) for CI, editors and agents. `--phase static|startup` selects a phase; exit codes are 0 clean, 1 violations, 2 the check could not run.

Statuses: **violated** (demonstrated), **unverifiable** (the check could not decide, for example a dynamic import, a rule that matched nothing, or a rule that crashed: this fails the build too, it is never a pass), **waived**.

## Exceptions (waivers)

`hinoki-rules.toml` next to the project:

```toml
[[waiver]]
rule = "HNK-ACC-001"
target = "tests.projects.rulesapp.health"     # fnmatch on the diagnostic's subject, bounded (not "*")
reason = "liveness probe is public by design"
owner = "platform-team"
expires = 2027-01-31                          # optional
```

```text
tests/projects/rulesapp.py:7: HNK-ACC-001 [waived] GET /health (health) declares no access policy
  fix: add access=<policy> to the action (or list it in except_ when it is public on purpose)
...
    waived: liveness probe is public by design (platform-team, until 2027-01-31)
2 rule(s): 2 violated, 0 unverifiable, 1 waived
```

A waiver needs a reason, an owner and a bounded target; an expired one stops applying and says so; a waiver that matches nothing is reported. A diagnostic that means "the rule could not do its job" cannot be waived. **Waivers are the owner's decision**: the tools read the file and never write it (put it under CODEOWNERS); `AGENTS.md` in a generated project tells agents the same.

## Your own rules

Subclass `hinoki.rules.Rule` with a stable `id`, a `phase` (`Phase.STARTUP` by default), an `explanation`, an optional `fix` line and `check(project)` yielding `Diagnostic`s (use `self.diagnostic(message, location, subject=...)`; `hinoki.rules.qualified(route)` gives an action's waiver subject). A rule that needs side effects (runs code, files, network, a database) declares them in `effects` and only runs with `check --allow-effects`.

```python
from hinoki.rules import Project, Rule, RuleSet, qualified


class NoDelete(Rule):
    id = "ACME-API-001"
    explanation = "This project archives rows instead of deleting them."
    fix = "use PATCH to set archived=true"

    def check(self, project: Project):
        for route in project.app.routes:
            if route.method == "DELETE":
                yield self.diagnostic(
                    f"DELETE {route.path} removes a row",
                    "api.py:1",
                    subject=qualified(route),
                )


rules = RuleSet(NoDelete())  # Hinoki(..., rules=rules)
```

```text
api.py:1: ACME-API-001 [violated] DELETE /things/{thing_id} removes a row
  fix: use PATCH to set archived=true
1 rule(s): 1 violated, 0 unverifiable, 0 waived
```

## Seeing them from an agent

The development MCP (`hinoki mcp`) exposes project inspection and rule diagnostics as read-only tools. See [development tools](ai-workflow.md). Rules inspect declarations and source structure; they do not prove what every function body does.
