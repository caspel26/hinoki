# 5. Rules

Rules turn promises into failures with a stable id and a location.

```python
tracker_rules = RuleSet(
    Actions.require_access(match="examples.tracker.*", except_=["examples.tracker.api.health"]),
    Actions.forbid_model_responses(match="examples.tracker.*"),
    Transactions.require_atomic(except_=["hinoki.mcpapp.*"]),
    Schemas.forbid_exposure("token"),
    # api -> operations -> models, never the other way
    Imports.forbid(source="examples.tracker.models", target="examples.tracker.operations"),
    Imports.forbid(source="examples.tracker.models", target="examples.tracker.api"),
    Imports.forbid(source="examples.tracker.operations", target="examples.tracker.api"),
)
```

Run them from the command line or let the app enforce them when it starts (`HinokiAPI(rules=tracker_rules)`):

```sh
python -m hinoki check --app examples.tracker.app:create_app \
    --rules examples.tracker.rules:tracker_rules --package examples
```

```text
7 rule(s): 0 violated, 0 unverifiable, 0 waived
```

Break one on purpose (remove `access=` from an action) and the failure names the rule and the line:

```text
tests/projects/rulesapp.py:7: HNK-ACC-001 [violated] GET /health (health) declares no access policy
tests/projects/rulesapp.py:12: HNK-TXN-001 [violated] POST /items (create) does not declare transaction=True
```

An exception is a **waiver** in `hinoki-rules.toml` with a reason, an owner and a bounded target. The owner writes waivers; tools and agents read them but never edit them to pass a check. See [Rules](../RULES.md).

Next: [6. Run and test it](6-run.md).
