"""The promises that keep the project in shape, run by ``hinoki check``.

Two ``RuleSet`` objects, both listed in ``[project] rules`` of hinoki.toml and
merged in that order: what an action must declare, and which layer may import
which.
"""

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
    Schemas.forbid_exposure("token_hash"),
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
