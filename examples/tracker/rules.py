"""The promises that keep the layers apart (run by ``hinoki check``)."""

from hinoki import Actions, Imports, Schemas, Transactions
from hinoki.rules import RuleSet

PKG = "tracker"

tracker_rules = RuleSet(
    Actions.require_access(match="tracker.*", except_=["tracker.api.health"]),
    Actions.forbid_model_responses(match=f"{PKG}.*"),
    # The MCP endpoint dispatches other actions, which have their own transactions.
    Transactions.require_atomic(except_=["hinoki.mcpapp.*"]),
    Schemas.forbid_exposure("token"),
    # The direction of dependencies: api -> operations -> models.
    Imports.forbid(source="tracker.models", target="tracker.operations"),
    Imports.forbid(source=f"{PKG}.models", target=f"{PKG}.api"),
    Imports.forbid(source=f"{PKG}.operations", target=f"{PKG}.api"),
)
