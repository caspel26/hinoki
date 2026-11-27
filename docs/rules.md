---
status: implemented
related:
  - authentication.md
  - transactions.md
  - inspect.md
  - ai-workflow.md
---

# Check architecture with rules

Rules are typed promises about your application: "every private action names a
policy", "writes are atomic", "no response leaks `password_hash`". Each one is a
Python object with a stable ID, deterministic behaviour and an exact fix line.
They are optional, and a project created with `hinoki new` starts with them on:

```python
# notes/rules.py
"""The project's rules. Weaken them only with the owner's decision (see hinoki-rules.toml)."""

from hinoki import rules

project_rules = rules.RuleSet(
    # Anything not public on purpose names an access policy, and the policy names permissions.
    rules.Actions.require_access(match="notes.*", except_=["notes.api.health"]),
    rules.Actions.require_permissions(match="notes.*", except_=["notes.api.health"]),
    # Responses are concrete Schema types and lists are paginated.
    rules.Actions.forbid_model_responses(match="notes.*"),
    rules.Actions.require_return_type(match="notes.*"),
    rules.Actions.require_pagination(match="notes.*"),
    # Writes run in a transaction that rolls back before errors become HTTP.
    rules.Transactions.require_atomic(),
    # Secrets never reach a client, and request text is bounded.
    rules.Schemas.forbid_exposure("password_hash", "password", "token"),
    rules.Schemas.require_string_limits(),
    # Turn these on with your first model (a model rule that matches no model
    # fails the check): tables are pinned and foreign keys are indexed.
    # rules.Models.require_explicit_table(match="notes.*"),
    # rules.Models.require_foreign_key_index(match="notes.*"),
)
```

`hinoki.toml` lists the set under `[project]`, and `Hinoki()` loads it (see
[which rules run](#which-rules-run)):

```toml
[project]
application = "notes.main:app"
rules = ["notes.rules:project_rules"]
```

Check it at any time:

```sh
hinoki check
```

## The twelve rules

| ID | Rule | Phase | Checks |
| --- | --- | --- | --- |
| `HNK-ACC-001` | `rules.Actions.require_access(match="*", except_=())` | startup | every matching action names an `Access` policy (public by default is the common hole) |
| `HNK-ACC-002` | `rules.Actions.require_permissions(match="*", except_=())` | startup | an action with an `Access` policy names at least one permission, so "any authenticated caller" is never implicit |
| `HNK-API-001` | `rules.Actions.forbid_model_responses(match="*", except_=())` | startup | no action returns a `Model` (or a list or page of them): responses are `Schema` classes |
| `HNK-API-002` | `rules.Actions.require_return_type(match="*", except_=())` | startup | every action annotates a concrete return type (not missing, `Any` or `object`; `None` is fine for a `204`) |
| `HNK-API-003` | `rules.Actions.require_pagination(match="*", except_=())` | startup | a `GET` action never returns a plain `list[...]` (alone or in a union): it returns a `Page` |
| `HNK-TXN-001` | `rules.Transactions.require_atomic(methods=("POST", "PUT", "PATCH", "DELETE"), match="*", except_=())` | startup | state-changing actions declare `atomic=True`, on the action or its `RouteGroup` |
| `HNK-SCH-001` | `rules.Schemas.forbid_exposure(*fields)` | startup | the named fields never appear in a response `Schema`: nested, in a subclass, in a union variant, as a field, an alias or a computed field; an action returning a `dict` is reported as unverifiable |
| `HNK-SCH-002` | `rules.Schemas.require_string_limits()` | startup | every text field a client sends (nested, in containers, `dict` values and union variants included) has `max_length` or `one_of`; `Enum` and `Literal` fields pass |
| `HNK-DB-001` | `rules.Models.require_explicit_table(match="*")` | startup | matching models pin their table with `@db.table(...)` or `table=`, so renaming the class cannot rename the table |
| `HNK-DB-002` | `rules.Models.require_foreign_key_index(match="*")` | startup | every foreign-key column leads an index, a primary key or a unique constraint |
| `HNK-MAIL-001` | `EmailTemplatesMatch()` | static | every registered [email template](email-templates.md) loads and matches its context `Schema` (opt-in, see [below](#an-opt-in-rule-email-templates)) |
| `HNK-IMP-001` | `rules.Imports.forbid(source, target)` | static | modules under `source` never import `target`, read from the source files; dynamic imports are reported, not ignored |

Every built-in rule has severity `error`. `HNK-API-002` matters most for actions
with `validate_response=False`: any other action with an unusable return
annotation already stops the app. `HNK-TXN-001` does not check the admin's own
routes, which open their own short transactions.

## An opt-in rule: email templates

`EmailTemplatesMatch()` (`HNK-MAIL-001`, phase `static`) is listed in the table but runs only when you put it in your `RuleSet`. It reads every registered [email template](email-templates.md) (the files included) and checks it against its context `Schema`, reporting one diagnostic per template that is refused. Because it reads files it declares the effect `files`, so `hinoki check` reports it as unverifiable unless you pass `--allow-effects`:

```python
from hinoki.rules import EmailTemplatesMatch, RuleSet

rules = RuleSet(EmailTemplatesMatch())
```

```sh
hinoki check --rules myproject.rules:rules --allow-effects
```

## Choose what a rule covers

Action rules match `fnmatch` patterns against an action's qualified name:
`module.function` for a function, `module.Class.method` for an API class (named
after the class it is mounted on), and `module.<Model>Crud.<action>` for a
`crud(...)` value, where `module` is the one that calls `crud(...)`. A bare `health` matches nothing; write
`notes.api.health` or `*.health`. `except_` takes the same patterns and is how
you say "public on purpose". The Blog example names its exceptions once:

```python
# examples/blog/blog/rules.py
from hinoki import rules

PUBLIC = [
    "blog.apps.blog.api.health",
    "blog.apps.blog.api.Posts.listing",
    "blog.apps.blog.api.Posts.one",
    "blog.apps.blog.accounts.*.login",  # public on purpose: it is how you sign in
]
SIGNED_IN = ["blog.apps.blog.accounts.*.me"]  # any signed-in user, no permission

blog_rules = rules.RuleSet(
    rules.Actions.require_access(match="blog.*", except_=PUBLIC),
    rules.Actions.require_permissions(match="blog.*", except_=[*PUBLIC, *SIGNED_IN]),
    rules.Transactions.require_atomic(except_=["hinoki.ai.mcpapp.*"]),
    rules.Schemas.forbid_exposure("token_hash", "password_hash"),
    rules.Imports.forbid(source="blog.apps.blog.models", target="blog.apps.blog.api"),
    # ... and the other six rules, see the file
)
```

Blog passes `blog_rules` and its waiver file to `Hinoki(...)` by hand. Tracker
lists two sets in its manifest instead,
`rules = ["tracker.rules:policy", "tracker.rules:layers"]`, and its
`Hinoki(...)` call names no rules.

The model rules match the model's *module* (`blog.*`) and report unverifiable
when no model matches, even with `*`. In a project with `hinoki.toml`,
`hinoki check` first imports the models of every `[[apps]]` entry, so they are
found without importing them from the application module. `require_access` and
`require_atomic`
report unverifiable when a pattern other than `*` matches no action. Neither
passes silently.

## Which rules run

The application's rules are, in this order of precedence:

1. `Hinoki(rules=...)`, when you pass it. An explicit argument wins, even an
   empty `RuleSet()`, and the manifest is not read for rules.
2. The `[project] rules` list of `hinoki.toml`, when `rules=` is omitted and the
   manifest's `project.application` names the module that builds the app.
3. Otherwise none.

`rules` is a list of `module:attribute` entries, each a `RuleSet`. The sets are
merged in the order listed into one `RuleSet`, so the report keeps that order.
The same rule (same id and summary) listed by two entries is a `ProjectError`
naming both entries, never a silent override; rules that share an id but differ
in their arguments, such as two `rules.Imports.forbid(...)`, are different rules. An entry that does not import, is not a `RuleSet`, or
a list of anything else is rejected the same way. You edit the list by hand;
no command writes it. `manifest_rules()` (from `hinoki.project`) is the explicit
form, for `Hinoki(rules=manifest_rules())` or a test.

`hinoki check` and `hinoki mcp` run `--rules MODULE:ATTRIBUTE` when you give it
and otherwise the app's own rules, which are the two sources above.

## When rules run

| Where | Rules | Waivers | On failure |
| --- | --- | --- | --- |
| Application startup (`hinoki dev`, `hinoki serve`, any ASGI server, the test clients) | startup rules of the [application's rules](#which-rules-run) | the [waiver file](#which-waiver-file) | startup fails with `RegistrationError` listing every failing diagnostic |
| `hinoki check` | both phases (or `--phase`) | the waiver file, or `--waivers` | exit code `1` |
| `hinoki mcp` | both phases | the waiver file, or `--waivers` | reported to the agent |

Startup enforcement means a broken promise never serves traffic. During a
migration you can pass `enforce_rules=False` to `Hinoki(...)`: violations are
then logged as a warning instead.

## Read the results

`hinoki check` prints one line per finding, with its location and a `fix:` hint.
This output comes from a deliberately broken project in Hinoki's own tests,
checked from a clone of the repository:

```sh
hinoki check tests.projects.rulesapp:create_app --rules tests.projects.rulesapp:rules
```

```text
tests/projects/rulesapp.py:7: HNK-ACC-001 [violated] GET /health (health) declares no access policy
  fix: add access=<policy> to the action (or list it in except_ when it is public on purpose)
tests/projects/rulesapp.py:12: HNK-ACC-001 [violated] POST /items (create) declares no access policy
  fix: add access=<policy> to the action (or list it in except_ when it is public on purpose)
tests/projects/rulesapp.py:12: HNK-TXN-001 [violated] POST /items (create) does not declare atomic=True
  fix: add atomic=True to the action (or declare it on a RouteGroup(..., atomic=True) group)
2 rule(s): 3 violated, 0 unverifiable, 0 waived
```

Each status means one thing: **violated** (demonstrated), **unverifiable** (the
check could not decide: a dynamic import, a pattern that matched nothing, a
return type that does not name its schema, or a rule that crashed), **waived**
and **warning** (a notice about the waiver file itself). Unverifiable fails like
a violation; it is never a pass. A warning never fails.

The fix line each rule prints:

| ID | `fix:` |
| --- | --- |
| `HNK-ACC-002` | name at least one permission in the Access policy of the action |
| `HNK-API-001` | return a Schema (for example PostOut.from_model(row)) instead of the Model |
| `HNK-API-002` | annotate the action's return type (a Schema, or None for status 204) |
| `HNK-API-003` | return Page[Item] or CursorPage[Item] and paginate the action |
| `HNK-SCH-001` | remove the field from the response Schema, or return a Schema that does not carry it |
| `HNK-SCH-002` | bound the field: Annotated[str, Field(max_length=...)] or one_of=... |
| `HNK-DB-001` | name the table with @db.table(...) or table=... on the model |
| `HNK-DB-002` | index the column: @db.indexed("<column>") on the model, or an index or unique group that starts with it |
| `HNK-IMP-001` | move the code to an allowed layer, or depend on it through the layer in between |

`hinoki check --format json` prints the same diagnostics as versioned data
(`schema_version`, `rules`, `summary`, and per diagnostic `rule`, `severity`,
`phase`, `status`, `message`, `location`, `explanation`, `subject`, `waiver`,
`fix`) for CI, editors and agents. `summary` counts each status (`violated`,
`unverifiable`, `waived`, `warning`) and adds `failing`, the number that sets
the exit code. `schema_version` is `2`: version 1 reported waiver notices as
`violated` and had no `warning` count. Exit codes: `0` clean, `1` failing, `2`
the check could not run. The other options are on
[Inspect an application](inspect.md#check).

## Accept an exception with a waiver

Prefer `except_` for things that are public or special *by design*. A waiver is
for a known violation the owner accepts for a while. It lives in
`hinoki-rules.toml` at the project root, which `hinoki new` creates:

```toml
# hinoki-rules.toml
[[waiver]]
rule = "HNK-ACC-001"
target = "tests.projects.rulesapp.health"
reason = "liveness probe is public by design"
owner = "platform-team"
expires = 2027-01-31
```

`rule`, `target`, `reason` and `owner` are required. `target` is an `fnmatch`
pattern on the diagnostic's subject and must be bounded: a literal prefix of at
least five characters containing a dot (`shop.api.*`, never `*`). `expires` is
optional and must be a TOML date, written without quotes; the waiver applies
through that day and expires once the current UTC date is later. The waived diagnostic
keeps its line and gains the decision:

```text
tests/projects/rulesapp.py:7: HNK-ACC-001 [waived] GET /health (health) declares no access policy
  fix: add access=<policy> to the action (or list it in except_ when it is public on purpose)
    waived: liveness probe is public by design (platform-team, until 2027-01-31)
```

An expired waiver stops applying and reports `HNK-WVR-002`; one that matches
nothing reports `HNK-WVR-001`. Both have status `warning`: they print as
`[warning]`, count under `warning(s)` in the summary (`summary.warning` in JSON)
and never change the exit code. A diagnostic that means "the rule
could not do its job" cannot be waived. An invalid or unreadable file makes
`hinoki check` exit with `2`.

### Which waiver file

`hinoki check`, `hinoki mcp` and application startup read the same file:

1. The file named by `Hinoki(waivers=...)`, or by `--waivers` on `check` and
   `mcp`. A named file must exist.
2. Otherwise `hinoki-rules.toml` at the project root: the directory of the
   nearest `hinoki.toml`, searched from the working directory upwards. Running
   from a subdirectory of the project finds the same file.
3. Without a `hinoki.toml`, `hinoki-rules.toml` in the working directory.

```python
app = Hinoki(rules=rules, waivers="config/hinoki-rules.toml")
```

A missing default file means no waivers. A directory or a broken symbolic link
with that name is an error, not an absent file. `hinoki mcp --waivers PATH` also
applies `PATH` to the app that `run_scenario` starts.

### Waivers at startup

Application startup applies the waiver file the same way as `hinoki check`.
This app requires access on every
action, yet it starts and serves `/health` because the owner waived it. It
passes `rules=` by hand, the explicit form, to keep the example in one file:

```python
# shop/main.py
from hinoki import Hinoki, Schema, http, rules


class Health(Schema):
    status: str


@http.get("/health")
def health() -> Health:
    return Health(status="ok")


app = Hinoki(rules=rules.RuleSet(rules.Actions.require_access()), include=[health])
```

```toml
# hinoki-rules.toml
[[waiver]]
rule = "HNK-ACC-001"
target = "shop.main.health"
reason = "liveness probe is public by design"
owner = "platform-team"
```

Without the waiver, `hinoki serve` fails at startup with
`RegistrationError: 1 rule violation(s)`.

At startup:

- An absent default file means no waivers.
- An invalid or unreadable file, or a missing file named with `waivers=`, fails
  startup with `RegistrationError` ("invalid waivers:
  .../hinoki-rules.toml: waiver #1: missing target"), even with
  `enforce_rules=False`.
- An expired waiver does not apply: the violation fails startup and the
  `RegistrationError` also lists the `HNK-WVR-002` notice. Without a remaining
  violation the notice is logged as a warning.
- `HNK-WVR-001` (unused waiver) is reported only by `hinoki check` and
  `hinoki mcp`: startup runs only the startup rules, so a waiver for a static
  rule such as `HNK-IMP-001` is expected to match nothing there.

Waivers are the owner's decision: tools read the file and never write it. Put it
under CODEOWNERS; the generated `AGENTS.md` tells agents the same.

## Your own rules

Subclass `hinoki.rules.Rule` with a stable `id`, an `explanation`, an optional
`fix` and a `check` that yields diagnostics. `phase` defaults to
`Phase.STARTUP` and `severity` to `Severity.ERROR`.

```python
# notes/rules.py
from collections.abc import Iterator

from hinoki.rules import Diagnostic, Project, Rule, qualified, where


class NoDelete(Rule):
    id = "NOTES-API-001"
    explanation = "This project archives rows instead of deleting them."
    fix = "use PATCH to set archived=true"

    def check(self, project: Project) -> Iterator[Diagnostic]:
        for route in project.app.routes:
            if route.method == "DELETE":
                yield self.diagnostic(
                    f"DELETE {route.path} removes a row",
                    where(route.handler),
                    subject=qualified(route),
                )
```

Add `NoDelete()` to the `RuleSet`. `where(obj)` gives a `file:line` location;
`qualified(route)` gives the subject that waivers match (without a subject the
location is used; an empty subject cannot be waived). A rule that needs side
effects (running code, files, network, a database) lists them in `effects` and
only runs under `hinoki check --allow-effects`; at startup it is refused and
reported as unverifiable. `RuleSet` raises `ValueError` for an empty ID or the
same ID twice.

## Errors and edge cases

- A rule that raises is reported as `unverifiable` with "the rule failed to run", never skipped.
- `HNK-IMP-001` runs only in `hinoki check` and `hinoki mcp`; it needs the source files of the checked packages (`--package`, default: the target's top-level package).
- The model rules in a project without models are unverifiable, which is why the scaffold keeps them commented out.

## See also

- [Inspect an application](inspect.md#check): every `hinoki check` option.
- [Authentication and access](authentication.md): what `HNK-ACC-001/002` ask for.
- [Build with a coding agent](ai-workflow.md): the same diagnostics for agents through `hinoki mcp`.
