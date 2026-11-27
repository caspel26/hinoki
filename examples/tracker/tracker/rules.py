"""The promises that keep the project in shape, run by ``hinoki check``.

Two ``RuleSet`` objects, both listed in ``[project] rules`` of hinoki.toml and
merged in that order: what an action must declare, and which layer may import
which.
"""

from hinoki import rules

PKG = "tracker"
TASKS = f"{PKG}.apps.tasks"

policy = rules.RuleSet(
    rules.Actions.require_access(match=f"{PKG}.*", except_=[f"{PKG}.routes.health"]),
    rules.Actions.require_permissions(
        match=f"{PKG}.*", except_=[f"{PKG}.routes.health"]
    ),
    rules.Actions.forbid_model_responses(match=f"{PKG}.*"),
    rules.Actions.require_return_type(match=f"{PKG}.*"),
    rules.Actions.require_pagination(match=f"{PKG}.*"),
    rules.Transactions.require_atomic(),
    rules.Schemas.forbid_exposure("token_hash"),
    rules.Schemas.require_string_limits(),
    rules.Models.require_explicit_table(match=f"{PKG}.*"),
    rules.Models.require_foreign_key_index(match=f"{PKG}.*"),
)

layers = rules.RuleSet(
    # api -> operations -> models, and tasks never imports its listener.
    rules.Imports.forbid(source=f"{TASKS}.models", target=f"{TASKS}.operations"),
    rules.Imports.forbid(source=f"{TASKS}.models", target=f"{TASKS}.api"),
    rules.Imports.forbid(source=f"{TASKS}.operations", target=f"{TASKS}.api"),
    rules.Imports.forbid(source=f"{TASKS}", target=f"{PKG}.apps.activity"),
)
