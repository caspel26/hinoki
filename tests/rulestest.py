"""Startup rules: typed, deterministic, optional."""

from uuid import UUID, uuid4

import pytest

from hinoki import (
    API,
    Access,
    Actions,
    HinokiAPI,
    RegistrationError,
    Request,
    Schema,
    Schemas,
    Transactions,
    get,
    post,
)
from hinoki.orm import Model, field
from hinoki.rules import (
    Diagnostic,
    Phase,
    Project,
    Rule,
    RuleSet,
    Severity,
    Status,
    failing,
)
from tests.providers import TokenAuth

editor = Access(auth=TokenAuth(), permissions=["posts:write"])


class Address(Schema):
    secret_note: str


class UserOut(Schema):
    name: str
    address: Address


class AccountOut(Schema):
    owner: UserOut
    password_hash: str


@get("/health")
def health(request: Request) -> dict[str, str]:
    return {}


class Posts(API, path="/posts"):
    @get("/")
    def listing(self, request: Request) -> list[UserOut]:
        return []

    @post("/", access=editor)
    def create(self, request: Request) -> AccountOut:
        raise NotImplementedError


class Safe(API, path="/safe"):
    @post("/", access=editor, transaction=True)
    def create(self, request: Request) -> dict[str, str]:
        return {}


def project(*items: object) -> Project:
    app = HinokiAPI()
    app.include(*items)
    return Project(app)


def ids(diagnostics: list[Diagnostic]) -> list[tuple[str, str]]:
    return [(d.rule_id, d.message.split(" ")[0]) for d in diagnostics]


def test_require_access_names_every_unprotected_action_with_its_location() -> None:
    rules = RuleSet(Actions.require_access(match="*Posts*"))
    found = rules.run(project(health, Posts))
    assert [d.rule_id for d in found] == ["HNK-ACC-001"]
    assert found[0].message.startswith("GET /posts")
    assert found[0].location.startswith("tests/rulestest.py:")
    assert found[0].status is Status.VIOLATED
    assert found[0].phase is Phase.STARTUP


def test_require_access_exceptions_and_patterns() -> None:
    everything = RuleSet(Actions.require_access())
    assert len(everything.run(project(health, Posts))) == 2
    allowed = RuleSet(Actions.require_access(except_=["*.health", "*.listing"]))
    assert allowed.run(project(health, Posts)) == []


def test_require_atomic_flags_writes_without_a_transaction() -> None:
    rules = RuleSet(Transactions.require_atomic())
    found = rules.run(project(Posts, Safe))
    assert [d.message.split(" (")[0] for d in found] == ["POST /posts"]
    assert (
        RuleSet(Transactions.require_atomic(methods=["GET"]))
        .run(project(health))[0]
        .rule_id
        == "HNK-TXN-001"
    )


def test_forbidden_fields_are_found_through_nested_schemas() -> None:
    rules = RuleSet(Schemas.forbid_exposure("password_hash", "secret_note"))
    found = rules.run(project(Posts))
    assert sorted(d.message.split(" is ")[0] for d in found) == [
        "AccountOut.password_hash",
        "Address.secret_note",
        "Address.secret_note",
    ]
    assert all(d.location.startswith("tests/rulestest.py:") for d in found)
    with pytest.raises(ValueError, match="at least one"):
        Schemas.forbid_exposure()


def test_enforcement_fails_the_application_with_every_violation() -> None:
    app = HinokiAPI(
        rules=RuleSet(Actions.require_access(), Transactions.require_atomic())
    )
    app.include(health, Posts)
    with pytest.raises(RegistrationError, match=r"3 rule violation") as raised:
        app.asgi()
    assert "HNK-ACC-001" in str(raised.value)
    assert "HNK-TXN-001" in str(raised.value)


def test_log_only_mode_serves_and_clean_projects_pass(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = HinokiAPI(rules=RuleSet(Actions.require_access()), enforce_rules=False)
    app.include(health)
    with caplog.at_level("WARNING", logger="hinoki"):
        assert app.asgi() is not None
    assert "HNK-ACC-001" in caplog.text
    clean = HinokiAPI(rules=RuleSet(Transactions.require_atomic()))
    clean.include(Safe)
    assert clean.asgi() is not None


def test_no_rules_means_nothing_runs() -> None:
    app = HinokiAPI()
    app.include(health)
    assert app.asgi() is not None


class Broken(Rule):
    id = "HNK-TST-001"
    explanation = "always crashes"

    def check(self, project: Project) -> list[Diagnostic]:
        raise RuntimeError("boom")


class Effectful(Rule):
    id = "HNK-TST-002"
    explanation = "runs code"
    effects = ("runs application code",)

    def check(self, project: Project) -> list[Diagnostic]:
        return [self.diagnostic("ran", "x")]


def test_a_crashing_rule_is_reported_and_does_not_hide_the_others() -> None:
    found = RuleSet(Broken(), Actions.require_access()).run(project(health))
    assert [d.rule_id for d in found] == ["HNK-TST-001", "HNK-ACC-001"]
    assert found[0].status is Status.UNVERIFIABLE
    assert "boom" in found[0].message
    assert failing(found) == found  # both are errors, neither is waived


def test_effectful_rules_are_refused_unless_allowed() -> None:
    rules = RuleSet(Effectful())
    skipped = rules.run(project(health))
    assert skipped[0].status is Status.UNVERIFIABLE
    assert "runs application code" in skipped[0].message
    allowed = rules.run(project(health), allow_effects=True)
    assert allowed[0].message == "ran"


def test_rule_sets_validate_their_rules() -> None:
    with pytest.raises(ValueError, match="stable id"):
        RuleSet(Rule())
    with pytest.raises(ValueError, match="twice"):
        RuleSet(Actions.require_access(), Actions.require_access())


def test_diagnostics_are_data_first() -> None:
    found = RuleSet(Actions.require_access()).run(project(health))[0]
    data = found.as_dict()
    assert set(data) == {
        "rule",
        "severity",
        "phase",
        "status",
        "message",
        "location",
        "explanation",
        "subject",
        "waiver",
    }
    assert data["severity"] == Severity.ERROR.value
    assert found.text().startswith(found.location)
    waived = Diagnostic(
        "X", Severity.ERROR, Phase.STARTUP, "m", "l", "e", Status.WAIVED
    )
    assert failing([waived]) == []


class Row(Model, table="rule_rows"):
    id: UUID = field(primary_key=True, default_factory=uuid4)


class RowOut(Schema):
    id: UUID


@get("/rows")
async def leaky(request: Request) -> list[Row]:
    return []


@get("/rows/one")
async def leaky_page(request: Request) -> Row | None:
    return None


@get("/rows/safe")
async def tidy(request: Request) -> list[RowOut]:
    return []


def test_forbid_model_responses_finds_models_through_containers() -> None:
    rules = RuleSet(Actions.forbid_model_responses())
    found = rules.run(project(leaky, leaky_page, tidy))
    assert [d.rule_id for d in found] == ["HNK-API-001", "HNK-API-001"]
    assert {d.message.split(" (")[0] for d in found} == {"GET /rows", "GET /rows/one"}
    assert "returns the model Row" in found[0].message
    exempt = RuleSet(Actions.forbid_model_responses(except_=["*.leaky*"]))
    assert exempt.run(project(leaky, leaky_page, tidy)) == []
