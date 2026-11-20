---
status: implemented
---

# 5. Rules

Rules turn promises into failures with a stable id and a location.

```python
from hinoki import Actions, Imports, Models, Schemas, Transactions
from hinoki.rules import RuleSet

PKG = "tracker"

tracker_rules = RuleSet(
    Actions.require_access(match="tracker.*", except_=["tracker.api.health"]),
    Actions.require_permissions(match="tracker.*", except_=["tracker.api.health"]),
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
    # api -> operations -> models, never the other way
    Imports.forbid(source="tracker.models", target="tracker.operations"),
    Imports.forbid(source=f"{PKG}.models", target=f"{PKG}.api"),
    Imports.forbid(source=f"{PKG}.operations", target=f"{PKG}.api"),
)
```

Run them from the command line or let the app enforce them when it starts (`Hinoki(rules=tracker_rules)`):

```sh
cd examples
uv run --project .. hinoki check tracker.app:create_app \
    --rules tracker.rules:tracker_rules --package tracker
```

```text
13 rule(s): 0 violated, 0 unverifiable, 0 waived
```

Break one on purpose (remove `access=` from an action) and the failure names the rule and the line:

```text
tests/projects/rulesapp.py:7: HNK-ACC-001 [violated] GET /health (health) declares no access policy
tests/projects/rulesapp.py:12: HNK-TXN-001 [violated] POST /items (create) does not declare atomic=True
```

An exception is a **waiver** in `hinoki-rules.toml` with a reason, an owner and a bounded target. The owner writes waivers; tools and agents read them but never edit them to pass a check. See [Rules](../rules.md).

Next: [6. Run and test it](6-run.md).
