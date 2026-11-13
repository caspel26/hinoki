# Hinoki's identity: proposals for the owner

Status: **proposals, not decisions** (Q026). Each item says what exists today, what is proposed and why. Nothing here is applied until the owner picks it; the roadmap in `handoff.md` records what was decided.

## What already makes Hinoki recognisable (keep and say it everywhere)

1. **It checks your architecture.** `python -m hinoki check` fails on a missing access policy, a write without a transaction, an exposed `password_hash`, a model returned from an action, an import against the layering. No other framework in this space ships that. *Voice:* "Hinoki refuses to ship what you promised not to."
2. **Policies and transactions read as declarations:** `access=member_only`, `transaction=True`, `errors=(NotFound,)`, groups with `Endpoints(...)`. Failures are declared, so OpenAPI, the MCP and the test client all know them.
3. **Built to be driven by an agent, with guardrails you can verify:** the development MCP, the application MCP with an allowlist, waivers only the owner can write.
4. **Rust where it matters, plain Python where you read it:** parsing, validation, JSON, hashing and the query compiler are native; the surface is ordinary typed Python, sync or async.
5. **Typed settings and secrets that never print.**
6. **An admin with three genuinely different styles.**

## Proposals

| # | Proposal | Today | Why |
| --- | --- | --- | --- |
| I1 | **Tagline and pitch** | "Typed Python APIs with a native Rust core." | Lead with the checkable architecture and the agent workflow, not the Rust: "A Python API framework that checks its own architecture." Rust is the *how*, not the promise. |
| I2 | **Vocabulary rule** | `Schema`, `Model`, `Access`, `Caller`, `Endpoints`, `Reply`; glossary in `docs/GLOSSARY.md` | One word per concept, ordinary English, no gimmick names. Identity comes from behaviour, not from renaming `Schema` (decided). |
| I3 | **The application object** | `HinokiAPI(...)` built by a factory (`create_app`) | Rename to **`Hinoki`** (`app = Hinoki(settings=...)`; `HinokiAPI` kept as an alias until the first release): shorter, owns the brand, reads well in `from hinoki import Hinoki`. Keep the factory for per-worker construction. |
| I4 | **Declaring the app as data** | `create_app()` function that includes and installs | Optional second form for small apps: `app = Hinoki(settings=BlogSettings, include=[...], modules=[...])`, loaded lazily by `hinoki serve blog.main:app`. Keep the factory form for tests and workers. |
| I5 | **Error voice** | `RegistrationError` messages name the action and the fix | Standardise: *what happened, where, the exact fix, the rule id* (`HNK-…`), one line each. Already true for registration errors and rules; extend to settings and migrations. |
| I6 | **Status words** | "Pre-alpha" | Keep it honest; add a public "what works today / what does not" table (README has it) and link it from every landing page. |
| I7 | **Visual identity** | violet mark and wordmark, three admin styles | Use the violet as the single brand colour in docs, CLI output and the admin default; give the docs a tree-ring motif (the hinoki is a cypress) instead of stock graphics. |
| I8 | **CLI voice** | `python -m hinoki ...` | Add a `hinoki` console script (`hinoki dev`, `hinoki serve`, `hinoki check`, `hinoki settings`, `hinoki new`) so docs and agents use one short command; `python -m hinoki` keeps working. |
| I9 | **Decorator vocabulary** | `@get/@post/...`, `@unique/@indexed/@ordered_by`, `@field_check/@object_check` | Keep: one visual grammar (decorators state facts about the thing below them). Do not add more declaration syntaxes. |
| I10 | **A signature example** | blog, tracker | Promote the tracker as *the* reference (layers, rules, errors, MCP) and keep the blog as the admin showcase. |

## What it would look like

**Today** (works now): a factory builds the app, `granian` or `hinoki dev` runs it.

```python
# blog/main.py
def create_app() -> HinokiAPI:
    settings = BlogSettings.load()
    app = HinokiAPI(settings=settings, title="Blog", rules=blog_rules)
    app.include(health, Posts, build_admin(settings).api())
    app.install(DatabaseModule(settings.database.url.reveal()))
    return app
```

```sh
python -m hinoki dev blog.main:create_app
```

**Proposed (I3 + I4 + I8)**: the app class carries the brand, small apps are declared as data, and one short command runs everything. Nothing below exists yet; the factory form keeps working.

```python
# blog/main.py
from hinoki import Hinoki

app = Hinoki(
    settings=BlogSettings,                 # a class: loaded when the worker starts
    title="Blog",
    rules=blog_rules,
    include=[health, Posts, admin],        # actions, API classes, admin
    modules=[DatabaseModule],              # built from the settings' database group
)
```

```sh
hinoki dev blog.main:app            # reload
hinoki serve blog.main:app          # host, port, workers from the settings
hinoki check blog.main:app          # the rules
hinoki settings blog.main:app       # where every value comes from
```

**Error voice (I5)**: what happened, where, the exact fix, the rule id.

```text
blog/api.py:41  HNK-ACC-001  DELETE /posts/{post_id} declares no access policy
  fix: add access=editor (or list it in except_ if it is public on purpose)
```

**Tagline (I1)**, on the landing page and in `pip` metadata: *"A Python API framework that checks its own architecture."*

## Decisions needed

I3 and I8 are small, mechanical and compatible (aliases stay). I4 and I1 are positioning choices. The rest are conventions already in the code; confirming them makes them a rule.
