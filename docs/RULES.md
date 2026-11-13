# Rules: architecture you can check

Rules are optional, typed promises about your application that a tool checks and that fail with a stable ID, a location and an explanation. They are not a linter for style and not an AI prompt: each rule is a Python object with deterministic behaviour. Nothing runs unless you enable rules.

## Enable them (the blog does)

```python
# examples/blog/rules.py
blog_rules = RuleSet(
    Actions.require_access(match="examples.blog.api.*", except_=PUBLIC),  # HNK-ACC-001
    Transactions.require_atomic(),                                         # HNK-TXN-001
    Schemas.forbid_exposure("token", "password_hash"),                     # HNK-SCH-001
    Imports.forbid(source="examples.blog.models", target="examples.blog.api"),  # HNK-IMP-001
)

# examples/blog/main.py
app = HinokiAPI(..., rules=blog_rules)   # startup rules fail the app if a promise is broken
```

| ID | Rule | Phase | Checks |
| --- | --- | --- | --- |
| `HNK-ACC-001` | `Actions.require_access(match, except_)` | startup | every matching action names an `Access` policy (public by default is the common hole) |
| `HNK-TXN-001` | `Transactions.require_atomic(methods)` | startup | state-changing actions use `transaction=True` |
| `HNK-SCH-001` | `Schemas.forbid_exposure(*fields)` | startup | named fields never appear in a response schema, nested or in a subclass |
| `HNK-IMP-001` | `Imports.forbid(source, target)` | static | modules under `source` never import `target` (read from the source files; dynamic imports are reported, not ignored) |

`match` and `except_` patterns use `fnmatch` on the action's full qualified name (`pkg.api.Posts.create`, named after the class it is mounted on). A bare name such as `health` matches nothing: write `*.health`.

## Run them

```sh
python -m hinoki check --app examples.blog.main:create_app \
    --rules examples.blog.rules:blog_rules --package examples
# 4 rule(s): 0 violated, 0 unverifiable, 0 waived        (exit 0)
```

A broken promise (a deliberately bad app from the tests):

```text
tests/projects/rulesapp.py:7: HNK-ACC-001 [violated] GET /health (health) declares no access policy
tests/projects/rulesapp.py:12: HNK-ACC-001 [violated] POST /items (create) declares no access policy
tests/projects/rulesapp.py:12: HNK-TXN-001 [violated] POST /items (create) does not declare transaction=True
2 rule(s): 3 violated, 0 unverifiable, 0 waived        (exit 1)
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

The development MCP (`python -m hinoki mcp`) exposes `list_rules`, `check_project` and `explain_error` (which also explains rule IDs), read only. See [AI-WORKFLOW.md](AI-WORKFLOW.md). Design and limits: [RULES-DESIGN.md](RULES-DESIGN.md) (rules decide declarations, not what a function body does; there are no runtime or scenario rules yet).
