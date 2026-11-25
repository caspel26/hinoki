---
status: implemented
related:
  - rules.md
  - patterns.md
---

# 5. Rules

Rules turn the project's promises into checks. A failure names the rule, the
file and the line.

The Tracker keeps its promises in `tracker/rules.py`, as two `RuleSet` values:
`policy` says what an action, a schema and a model must declare, and `layers`
says which module may import which.

```python
# tracker/rules.py
from hinoki import Actions, Imports, Models, Schemas, Transactions
from hinoki.rules import RuleSet

PKG = "tracker"
TASKS = f"{PKG}.apps.tasks"

policy = RuleSet(
    Actions.require_access(match=f"{PKG}.*", except_=[f"{PKG}.routes.health"]),
    Actions.require_permissions(match=f"{PKG}.*", except_=[f"{PKG}.routes.health"]),
    Actions.forbid_model_responses(match=f"{PKG}.*"),
    Actions.require_return_type(match=f"{PKG}.*"),
    Actions.require_pagination(match=f"{PKG}.*"),
    Transactions.require_atomic(),
    Schemas.forbid_exposure("token"),
    Schemas.require_string_limits(),
    Models.require_explicit_table(match=f"{PKG}.*"),
    Models.require_foreign_key_index(match=f"{PKG}.*"),
)

layers = RuleSet(
    # api -> operations -> models, and tasks never imports its listener.
    Imports.forbid(source=f"{TASKS}.models", target=f"{TASKS}.operations"),
    Imports.forbid(source=f"{TASKS}.models", target=f"{TASKS}.api"),
    Imports.forbid(source=f"{TASKS}.operations", target=f"{TASKS}.api"),
    Imports.forbid(source=f"{TASKS}", target=f"{PKG}.apps.activity"),
)
```

`hinoki.toml` lists both under `rules` in `[project]`, and the application
merges them in that order, so a worker refuses to start while one is violated.
`main.py` passes no `rules=` argument ([chapter 6](6-run.md)).

## Run the check

From `examples/tracker`:

```sh
uv run hinoki check
```

```text
14 rule(s): 0 violated, 0 unverifiable, 0 waived
```

The check reads the rules from the manifest, imports the models of every app
and scans the `tracker` package for the import rules.

## Break a rule

Take `complete` out of its transaction by changing `atomic=True` to
`atomic=False` in its decorator in `tracker/apps/tasks/api.py`, then run the
check again:

```text
tracker/apps/tasks/api.py:39: HNK-TXN-001 [violated] POST /tasks/{item_id}/complete (Tasks.complete) does not declare atomic=True
  fix: add atomic=True to the action (or declare it on a RouteGroup(..., atomic=True) group)
14 rule(s): 1 violated, 0 unverifiable, 0 waived
```

The command exits with status 1. Put `atomic=True` back to restore the check.

The import rules fail the same way. Add `from tracker.apps.activity import models`
as the last line of `tracker/apps/tasks/operations.py` and the check reports it:

```text
tracker/apps/tasks/operations.py:46: HNK-IMP-001 [violated] tracker.apps.tasks.operations imports tracker.apps.activity, which is forbidden for tracker.apps.tasks
  fix: move the code to an allowed layer, or depend on it through the layer in between
```

(the check also lists the same line for `tracker.apps.activity.models`, which
brings the total to `2 violated`). Remove the import to restore it.

## Waivers

An accepted exception is a **waiver** in `hinoki-rules.toml`, with a reason, an
owner and a bounded target. `hinoki check` and application startup apply the
same waivers. The project owner writes waivers; tools and agents read them but
never edit them to make a check pass. The Tracker needs none; the
[Blog example](https://github.com/caspel26/hinoki/blob/main/examples/blog/hinoki-rules.toml)
carries one.

## Recap

- Rules are Python values in the project, checked by `hinoki check` and at startup.
- Each failure has a stable id, a location and a fix.
- Exceptions are explicit, owned waivers.

Next: [6. Run and test it](6-run.md).
