"""An application that deliberately breaks two rules, for the check CLI tests."""

from hinoki import Actions, HinokiAPI, Request, Transactions, get, post
from hinoki.rules import RuleSet


@get("/health")
def health(request: Request) -> dict[str, str]:
    return {}


@post("/items")
def create(request: Request) -> dict[str, str]:
    return {}


rules = RuleSet(Actions.require_access(), Transactions.require_atomic())


def create_app() -> HinokiAPI:
    app = HinokiAPI()
    app.include(health, create)
    return app
