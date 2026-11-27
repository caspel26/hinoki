"""An application that deliberately breaks two rules, for the check CLI tests."""

from hinoki import Hinoki, Request, http
from hinoki.rules import Actions, Diagnostic, Project, Rule, RuleSet, Transactions


@http.get("/health")
def health(request: Request) -> dict[str, str]:
    return {}


@http.post("/items")
def create(request: Request) -> dict[str, str]:
    return {}


rules = RuleSet(Actions.require_access(), Transactions.require_atomic())


def create_app() -> Hinoki:
    app = Hinoki()
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
