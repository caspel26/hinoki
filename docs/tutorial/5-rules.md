---
status: implemented
related:
  - rules.md
  - patterns.md
---

# 5. Rules

Rules turn the project's promises into checks. A failure names the rule, the
file and the line.

```python
# tracker/rules.py
from hinoki import Actions, Imports, Models, Schemas, Transactions
from hinoki.rules import RuleSet

PKG = "tracker"
APP = f"{PKG}.apps.tracker"  # the app that owns the models, operations and api

tracker_rules = RuleSet(
    Actions.require_access(match=f"{PKG}.*", except_=[f"{APP}.api.health"]),
    Actions.require_permissions(match=f"{PKG}.*", except_=[f"{APP}.api.health"]),
    Actions.forbid_model_responses(match=f"{PKG}.*"),
    Actions.require_return_type(match=f"{PKG}.*"),
    Actions.require_pagination(match=f"{PKG}.*"),
    # The MCP endpoint dispatches other actions, which have their own transactions.
    Transactions.require_atomic(except_=["hinoki.ai.mcpapp.*"]),
    Schemas.forbid_exposure("token"),
    Schemas.require_string_limits(),
    # Tables are pinned, and a foreign key is always indexed.
    Models.require_explicit_table(match=f"{PKG}.*"),
    Models.require_foreign_key_index(match=f"{PKG}.*"),
    # The direction of dependencies: api -> operations -> models.
    Imports.forbid(source=f"{APP}.models", target=f"{APP}.operations"),
    Imports.forbid(source=f"{APP}.models", target=f"{APP}.api"),
    Imports.forbid(source=f"{APP}.operations", target=f"{APP}.api"),
)
```

The application passes them to `Hinoki(rules=tracker_rules)`, so a worker
refuses to start while one is violated.

## Run the check

From `examples/`:

```sh
uv run hinoki check tracker.main:app
```

```text
13 rule(s): 0 violated, 0 unverifiable, 0 waived
```

The check reads the rules from the application and scans the `tracker`
package for the import rules.

## Break a rule

Take `create_task` out of the group's transaction by adding `atomic=False` to its
decorator, then run the check again:

```text
tracker/apps/tracker/api.py:29: HNK-TXN-001 [violated] POST /tasks (create_task) does not declare atomic=True
  fix: add atomic=True to the action (or declare it on a RouteGroup(..., atomic=True) group)
13 rule(s): 1 violated, 0 unverifiable, 0 waived
```

The command exits with status 1. Remove `atomic=False` to restore the check.

## Waivers

An accepted exception is a **waiver** in `hinoki-rules.toml`, with a reason, an
owner and a bounded target. `hinoki check` and application startup apply the
same waivers. The project owner writes waivers; tools and agents read them but
never edit them to make a check pass.

## Recap

- Rules are Python values in the project, checked by `hinoki check` and at startup.
- Each failure has a stable id, a location and a fix.
- Exceptions are explicit, owned waivers.

Next: [6. Run and test it](6-run.md).
