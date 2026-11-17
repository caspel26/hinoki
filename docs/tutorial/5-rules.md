---
status: implemented
---

# 5. Rules

Rules turn promises into failures with a stable id and a location.

```python
from hinoki import Actions, Imports, Schemas, Transactions
from hinoki.rules import RuleSet


tracker_rules = RuleSet(
    Actions.require_access(match="tracker.*", except_=["tracker.api.health"]),
    Actions.forbid_model_responses(match="tracker.*"),
    Transactions.require_atomic(except_=["hinoki.mcpapp.*"]),
    Schemas.forbid_exposure("token"),
    # api -> operations -> models, never the other way
    Imports.forbid(source="tracker.models", target="tracker.operations"),
    Imports.forbid(source="tracker.models", target="tracker.api"),
    Imports.forbid(source="tracker.operations", target="tracker.api"),
)
```

Run them from the command line or let the app enforce them when it starts (`Hinoki(rules=tracker_rules)`):

```sh
cd examples
uv run --project .. hinoki check tracker.app:create_app \
    --rules tracker.rules:tracker_rules --package tracker
```

```text
7 rule(s): 0 violated, 0 unverifiable, 0 waived
```

Break one on purpose (remove `access=` from an action) and the failure names the rule and the line:

```text
tests/projects/rulesapp.py:7: HNK-ACC-001 [violated] GET /health (health) declares no access policy
tests/projects/rulesapp.py:12: HNK-TXN-001 [violated] POST /items (create) does not declare transaction=True
```

An exception is a **waiver** in `hinoki-rules.toml` with a reason, an owner and a bounded target. The owner writes waivers; tools and agents read them but never edit them to pass a check. See [Rules](../rules.md).

Next: [6. Run and test it](6-run.md).
