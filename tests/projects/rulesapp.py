"""An application that deliberately breaks two rules, for the check CLI tests."""

from hinoki import Actions, HinokiAPI, Request, Transactions, get, post
from hinoki.rules import Diagnostic, Project, Rule, RuleSet


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


class Chatty(Rule):
    id = "HNK-TST-020"
    explanation = "prints while running"

    def check(self, project: Project) -> list[Diagnostic]:
        print("debug from a rule")
        return []


class Effectful(Rule):
    id = "HNK-TST-021"
    explanation = "pretends to need a database"
    effects = ("database",)

    def check(self, project: Project) -> list[Diagnostic]:
        return []


chatty = RuleSet(Chatty())
effectful = RuleSet(Effectful())
