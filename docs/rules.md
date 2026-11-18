---
status: implemented
related:
  - ai-workflow.md
  - authentication.md
  - transactions.md
---

# Rules: architecture you can check

Rules are optional, typed promises about your application that a tool checks and that fail with a stable ID, a location and an explanation. They are not a linter for style and not an AI prompt: each rule is a Python object with deterministic behaviour. Nothing runs unless you enable rules.

The built-in set is intentionally small: access declarations, atomic writes, response types, model responses, pagination, permissions, request text limits, table names, foreign-key indexes, schema exposure and import direction. Add a custom `Rule` only when a project has a concrete invariant that these checks cannot express.

## Enable them (the blog and the tracker enable all of them)

```python
# examples/blog/rules.py
blog_rules = RuleSet(
    Actions.require_access(match="blog.*", except_=PUBLIC),            # HNK-ACC-001
    Actions.require_permissions(match="blog.*", except_=PUBLIC),       # HNK-ACC-002
    Actions.forbid_model_responses(match="blog.*"),                    # HNK-API-001
    Actions.require_return_type(match="blog.*"),                       # HNK-API-002
    Actions.require_pagination(match="blog.*"),                        # HNK-API-003
    Transactions.require_atomic(except_=["blog.accounts.*.login"]),    # HNK-TXN-001
    Schemas.forbid_exposure("token", "password_hash"),                 # HNK-SCH-001
    Schemas.require_string_limits(),                                   # HNK-SCH-002
    Models.require_explicit_table(match="blog.*"),                     # HNK-DB-001
    Models.require_foreign_key_index(match="blog.*"),                  # HNK-DB-002
    Imports.forbid(source="blog.models", target="blog.api"),           # HNK-IMP-001
)

# examples/blog/main.py
app = Hinoki(..., rules=blog_rules)   # startup rules fail the app if a promise is broken
```

| ID | Rule | Phase | Checks |
| --- | --- | --- | --- |
| `HNK-ACC-001` | `Actions.require_access(match, except_)` | startup | every matching action names an `Access` policy (public by default is the common hole) |
| `HNK-ACC-002` | `Actions.require_permissions(match, except_)` | startup | an action that names an `Access` policy names at least one permission, so "any authenticated caller" is never implicit |
| `HNK-TXN-001` | `Transactions.require_atomic(methods, match, except_)` | startup | state-changing actions use `transaction=True` |
| `HNK-API-001` | `Actions.forbid_model_responses(match, except_)` | startup | no action returns a `Model` (or a list/page of them): responses are `Schema` classes |
| `HNK-API-002` | `Actions.require_return_type(match, except_)` | startup | every matching action annotates a concrete return type (not missing, `Any` or `object`; `None` is fine for a 204) |
| `HNK-API-003` | `Actions.require_pagination(match, except_)` | startup | a `GET` action never returns a plain `list[...]` (alone or in a union): it returns a `Page` |
| `HNK-SCH-001` | `Schemas.forbid_exposure(*fields)` | startup | named fields never appear in a response `Schema`, nested or in a subclass |
| `HNK-SCH-002` | `Schemas.require_string_limits()` | startup | every text field of a request `Schema` (nested ones included, and the elements of a `list[str]`) has `max_length` or `one_of` |
| `HNK-DB-001` | `Models.require_explicit_table(match)` | startup | models whose module matches `match` pin their table with `@db.table(...)` or `table=`; a rename of the class then cannot change the physical table |
| `HNK-DB-002` | `Models.require_foreign_key_index(match)` | startup | every foreign-key column of a matching model leads an index, a primary key or a unique constraint |
| `HNK-IMP-001` | `Imports.forbid(source, target)` | static | modules under `source` never import `target` (read from the source files; dynamic imports are reported, not ignored) |

The model rules take `match` as an `fnmatch` pattern on the model's module (`blog.*`); a pattern that matches no model is reported as unverifiable.

`match` and `except_` patterns on action rules use `fnmatch` on the action's full qualified name (`pkg.api.Posts.create`, named after the class it is mounted on). A bare name such as `health` matches nothing: write `*.health`.

## Run them

The Blog target is part of the checked-in project in `examples/`. Run this from
that directory, using the locked environment in the parent directory:

```sh
cd examples
uv run --project .. hinoki check blog.main:create_app \
    --rules blog.rules:blog_rules --package blog
# 11 rule(s): 0 violated, 0 unverifiable, 0 waived       (exit 0)
```

A broken promise (a deliberately bad app from the tests):

```text
tests/projects/rulesapp.py:7: HNK-ACC-001 [violated] GET /health (health) declares no access policy
tests/projects/rulesapp.py:12: HNK-ACC-001 [violated] POST /items (create) declares no access policy
tests/projects/rulesapp.py:12: HNK-TXN-001 [violated] POST /items (create) does not declare transaction=True
2 rule(s): 3 violated, 0 unverifiable, 0 waived        (exit 1)
```

The other rules report the same way. This output comes from a small project whose `api.py` returns a `Model`, exposes `token` in a response `Schema`, and whose `models.py` imports `api`:

```text
shop/api.py:13: HNK-API-001 [violated] GET /members/{member_id} (one) returns the model Member; return a Schema
  fix: return a Schema (for example PostOut.from_model(row))
shop/api.py:7: HNK-SCH-001 [violated] MemberOut.token is exposed by GET /members
  fix: remove the field from the response Schema, or return a Schema that does not carry it
shop/models.py:11: HNK-IMP-001 [violated] shop.models imports shop.api, which is forbidden for shop.models
  fix: move the code to an allowed layer, or depend on it through the layer in between
3 rule(s): 3 violated, 0 unverifiable, 0 waived        (exit 1)
```

`--format json` prints the same diagnostics as versioned data (`schema_version`, `summary`, and per diagnostic `rule`, `severity`, `phase`, `status`, `message`, `location`, `explanation`, `subject`, `waiver`) for CI, editors and agents. `--phase static|startup` selects a phase; exit codes are 0 clean, 1 violations, 2 the check could not run.

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
    waived: liveness probe is public by design (platform-team, until 2027-01-31)
```

A waiver needs a reason, an owner and a bounded target; an expired one stops applying and says so; a waiver that matches nothing is reported. A diagnostic that means "the rule could not do its job" cannot be waived. **Waivers are the owner's decision**: the tools read the file and never write it (put it under CODEOWNERS); `AGENTS.md` in a generated project tells agents the same.

## Your own rules

Subclass `hinoki.rules.Rule` with a stable `id`, a `phase`, an `explanation` and `check(project)` yielding `Diagnostic`s (use `self.diagnostic(message, location, subject=...)`). A rule that needs side effects (runs code, files, network, a database) declares them in `effects` and only runs with `check --allow-effects`.

## Seeing them from an agent

The development MCP (`hinoki mcp`) exposes project inspection and rule diagnostics as read-only tools. See [development tools](ai-workflow.md). Rules inspect declarations and source structure; they do not prove what every function body does.
