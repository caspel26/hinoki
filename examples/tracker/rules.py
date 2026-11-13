"""The promises that keep the layers apart (run by ``python -m hinoki check``)."""

from hinoki import Actions, Imports, Schemas, Transactions
from hinoki.rules import RuleSet

PKG = "examples.tracker"

tracker_rules = RuleSet(
    Actions.require_access(
        match="examples.tracker.*", except_=["examples.tracker.api.health"]
    ),
    Actions.forbid_model_responses(match=f"{PKG}.*"),
    Transactions.require_atomic(),
    Schemas.forbid_exposure("token"),
    # The direction of dependencies: api -> operations -> models.
    Imports.forbid(
        source="examples.tracker.models", target="examples.tracker.operations"
    ),
    Imports.forbid(source=f"{PKG}.models", target=f"{PKG}.api"),
    Imports.forbid(source=f"{PKG}.operations", target=f"{PKG}.api"),
)
