"""The promises that keep the layers apart (run by ``hinoki check``)."""

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
