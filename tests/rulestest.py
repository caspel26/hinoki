"""Startup rules: typed, deterministic, optional."""

from pathlib import Path
from typing import Annotated, Any
from uuid import UUID, uuid4

import pytest

from hinoki import (
    API,
    Access,
    Field,
    Hinoki,
    Request,
    Schema,
    db,
    http,
)
from hinoki.errors import RegistrationError
from hinoki.orm import Model, OnDelete, field, foreign_key
from hinoki.pagination import Page
from hinoki.rules import (
    Actions,
    Diagnostic,
    Models,
    Phase,
    Project,
    Rule,
    RuleSet,
    Schemas,
    Severity,
    Status,
    Transactions,
    failing,
    is_open,
    reachable,
    returns_plain_list,
    where,
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


@http.get("/health")
def health(request: Request) -> dict[str, str]:
    return {}


class Posts(API, path="/posts"):
    @http.get("/")
    def listing(self, request: Request) -> list[UserOut]:
        return []

    @http.post("/", access=editor)
    def create(self, request: Request) -> AccountOut:
        raise NotImplementedError


class Safe(API, path="/safe"):
    @http.post("/", access=editor, atomic=True)
    def create(self, request: Request) -> dict[str, str]:
        return {}


def project(*items: object) -> Project:
    app = Hinoki()
    app.include(*items)  # type: ignore[arg-type]
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


def test_rule_location_and_open_annotation_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert where(42) == "42"
    monkeypatch.setattr("hinoki.rules.inspect.getsourcefile", lambda _target: None)
    monkeypatch.setattr("hinoki.rules.inspect.getsourcelines", lambda _target: ([], 1))
    assert where(object()).startswith("<object object at")
    assert is_open(None) and is_open(Any) and is_open(object)
    with pytest.raises(NotImplementedError):
        Rule().check(project())


def test_reachable_schema_graph_deduplicates_repeated_roots() -> None:
    assert list(reachable([Address, Address])) == [Address]


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
    app = Hinoki(rules=RuleSet(Actions.require_access(), Transactions.require_atomic()))
    app.include(health, Posts)
    with pytest.raises(RegistrationError, match=r"3 rule violation") as raised:
        app.asgi()
    assert "HNK-ACC-001" in str(raised.value)
    assert "HNK-TXN-001" in str(raised.value)


def test_log_only_mode_serves_and_clean_projects_pass(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = Hinoki(rules=RuleSet(Actions.require_access()), enforce_rules=False)
    app.include(health)
    with caplog.at_level("WARNING", logger="hinoki"):
        assert app.asgi() is not None
    assert "HNK-ACC-001" in caplog.text
    clean = Hinoki(rules=RuleSet(Transactions.require_atomic()))
    clean.include(Safe)
    assert clean.asgi() is not None


def test_no_rules_means_nothing_runs() -> None:
    app = Hinoki()
    app.include(health)
    assert app.asgi() is not None


def waiver_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    (tmp_path / "hinoki-rules.toml").write_text(text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)


HEALTH_WAIVER = (
    '[[waiver]]\nrule = "HNK-ACC-001"\ntarget = "tests.rulestest.health"\n'
    'reason = "liveness probe"\nowner = "platform"\n'
)


def test_startup_applies_the_waivers_check_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiver_file(tmp_path, monkeypatch, HEALTH_WAIVER)
    app = Hinoki(rules=RuleSet(Actions.require_access()))
    app.include(health)
    assert app.asgi() is not None


def test_an_expired_waiver_does_not_apply_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiver_file(tmp_path, monkeypatch, HEALTH_WAIVER + "expires = 2020-01-31\n")
    app = Hinoki(rules=RuleSet(Actions.require_access()))
    app.include(health)
    with pytest.raises(RegistrationError, match="HNK-ACC-001") as raised:
        app.asgi()
    assert "HNK-WVR-002" in str(raised.value)
    assert "expired on 2020-01-31" in str(raised.value)


def test_an_invalid_waiver_file_fails_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = HEALTH_WAIVER.replace('target = "tests.', 'x = "')
    waiver_file(tmp_path, monkeypatch, broken)
    app = Hinoki(rules=RuleSet(Actions.require_access()), enforce_rules=False)
    app.include(health)
    with pytest.raises(RegistrationError, match=r"hinoki-rules\.toml.*missing target"):
        app.asgi()


def test_startup_reads_the_waivers_of_the_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "hinoki.toml").write_text('[project]\napplication = "shop.main:app"\n')
    (tmp_path / "hinoki-rules.toml").write_text(HEALTH_WAIVER)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "hinoki-rules.toml").write_text("[[waiver")  # stray: never read
    monkeypatch.chdir(sub)
    app = Hinoki(rules=RuleSet(Actions.require_access()))
    app.include(health)
    assert app.asgi() is not None


def test_an_explicit_waiver_file_wins_and_must_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # no hinoki-rules.toml here
    named = tmp_path / "owner-waivers.toml"
    named.write_text(HEALTH_WAIVER)
    app = Hinoki(rules=RuleSet(Actions.require_access()), waivers=str(named))
    app.include(health)
    assert app.asgi() is not None
    missing = Hinoki(rules=RuleSet(Actions.require_access()), waivers=tmp_path / "no")
    missing.include(health)
    with pytest.raises(RegistrationError, match="does not exist"):
        missing.asgi()


def test_a_directory_named_like_the_waiver_file_fails_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "hinoki-rules.toml").mkdir()
    monkeypatch.chdir(tmp_path)
    app = Hinoki(rules=RuleSet(Actions.require_access()))
    app.include(health)
    with pytest.raises(RegistrationError, match="not a regular file"):
        app.asgi()


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
        "fix",
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


@http.get("/rows")
async def leaky(request: Request) -> list[Row]:
    return []


@http.get("/rows/one")
async def leaky_page(request: Request) -> Row | None:
    return None


@http.get("/rows/safe")
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


@http.get("/typed/untyped", validate_response=False)
def untyped(request: Request):  # type: ignore[no-untyped-def]
    return {}


@http.get("/typed/any", validate_response=False)
def any_return(request: Request) -> Any:
    return {}


@http.delete("/typed/gone", status=204)
def gone(request: Request) -> None:
    return None


def test_require_return_type_flags_missing_and_open_annotations() -> None:
    rules = RuleSet(Actions.require_return_type())
    found = rules.run(project(untyped, any_return, gone, health))
    assert [d.rule_id for d in found] == ["HNK-API-002", "HNK-API-002"]
    assert {d.message.split(" (")[0] for d in found} == {
        "GET /typed/untyped",
        "GET /typed/any",
    }
    assert all("concrete return type" in d.message for d in found)
    assert all(d.status is Status.VIOLATED for d in found)
    exempt = RuleSet(Actions.require_return_type(except_=["*.untyped", "*.any_return"]))
    assert exempt.run(project(untyped, any_return, gone)) == []
    scoped = RuleSet(Actions.require_return_type(match="*.any_return"))
    assert len(scoped.run(project(untyped, any_return))) == 1


@http.get("/lists/all")
def everything(request: Request) -> list[UserOut]:
    return []


@http.get("/lists/maybe")
def maybe_list(request: Request) -> list[UserOut] | None:
    return None


@http.get("/lists/paged")
def paged(request: Request) -> Page[UserOut]:
    raise NotImplementedError


@http.post("/lists/make")
def make_many(request: Request) -> list[UserOut]:
    return []


def test_require_pagination_flags_get_actions_returning_plain_lists() -> None:
    rules = RuleSet(Actions.require_pagination())
    found = rules.run(project(everything, maybe_list, paged, make_many, health))
    assert [d.rule_id for d in found] == ["HNK-API-003", "HNK-API-003"]
    assert {d.message.split(" (")[0] for d in found} == {
        "GET /lists/all",
        "GET /lists/maybe",
    }
    exempt = RuleSet(
        Actions.require_pagination(except_=["*.everything", "*.maybe_list"])
    )
    assert exempt.run(project(everything, maybe_list, paged)) == []
    scoped = RuleSet(Actions.require_pagination(match="*.paged"))
    assert scoped.run(project(everything, paged)) == []
    assert (
        RuleSet(Actions.require_pagination()).run(project(Posts))[0].rule_id
        == "HNK-API-003"
    )
    assert returns_plain_list(list) and not returns_plain_list(None)


viewer_only = Access(auth=TokenAuth())


@http.get("/perm/open", access=viewer_only)
def any_caller(request: Request) -> dict[str, str]:
    return {}


@http.get("/perm/scoped", access=editor)
def scoped(request: Request) -> dict[str, str]:
    return {}


def test_require_permissions_flags_policies_without_permissions() -> None:
    rules = RuleSet(Actions.require_permissions())
    found = rules.run(project(any_caller, scoped, health))
    assert [d.rule_id for d in found] == ["HNK-ACC-002"]
    assert found[0].message.startswith("GET /perm/open")
    exempt = RuleSet(Actions.require_permissions(except_=["*.any_caller"]))
    assert exempt.run(project(any_caller, scoped)) == []
    scoped_only = RuleSet(Actions.require_permissions(match="*.scoped"))
    assert scoped_only.run(project(any_caller, scoped)) == []


class LimitedIn(Schema):
    title: Annotated[str, Field(max_length=40)]
    kind: Annotated[str, Field(one_of=("a", "b"))]
    count: int = 0


class FreeIn(Schema):
    title: str
    note: Annotated[str, Field(max_length=10)] = ""


class WrapperIn(Schema):
    inner: FreeIn
    count: int = 0


@http.post("/limits/ok")
def limited(request: Request, body: LimitedIn) -> dict[str, str]:
    return {}


@http.post("/limits/free")
def free_text(request: Request, body: WrapperIn) -> dict[str, str]:
    return {}


@http.post("/limits/free-again")
def free_text_again(request: Request, body: FreeIn) -> dict[str, str]:
    return {}


def test_require_string_limits_flags_unbounded_request_text() -> None:
    rules = RuleSet(Schemas.require_string_limits())
    assert rules.run(project(limited)) == []
    found = rules.run(project(free_text, free_text_again))
    assert [d.rule_id for d in found] == ["HNK-SCH-002"]
    assert found[0].message.startswith("FreeIn.title is read from POST /limits/")
    assert found[0].subject.endswith("FreeIn.title")
    assert RuleSet(Schemas.require_string_limits()).run(project(Posts)) == []


class ListsIn(Schema):
    tags: list[str]
    bounded: list[Annotated[str, Field(max_length=10)]]
    nested: list[list[str]]


@http.post("/limits/lists")
def lists(request: Request, body: ListsIn) -> dict[str, str]:
    return {}


def test_require_string_limits_looks_inside_lists() -> None:
    found = RuleSet(Schemas.require_string_limits()).run(project(lists))
    assert sorted(d.subject.rsplit(".", 1)[1] for d in found) == ["nested", "tags"]


def test_require_atomic_with_different_exceptions_are_different_rules() -> None:
    RuleSet(
        Transactions.require_atomic(except_=["*.a"]),
        Transactions.require_atomic(except_=["*.b"]),
    )
    with pytest.raises(ValueError, match="listed twice"):
        RuleSet(Transactions.require_atomic(), Transactions.require_atomic())


def test_require_atomic_match_limits_the_actions_checked() -> None:
    assert (
        RuleSet(Transactions.require_atomic(match="other.*"))
        .run(project(Posts))[0]
        .status.value
        == "unverifiable"
    )
    found = RuleSet(Transactions.require_atomic(match="tests.*")).run(project(Posts))
    assert {d.rule_id for d in found} == {"HNK-TXN-001"}


class RulesParent(Model, table="rules_parents"):
    id: int = field(primary_key=True)


class RulesLoose(Model, table="rules_loose"):
    id: int = field(primary_key=True)
    parent_id: int = foreign_key(RulesParent, on_delete=OnDelete.RESTRICT)


@db.indexed("parent_id")
class RulesIndexed(Model, table="rules_indexed"):
    id: int = field(primary_key=True)
    parent_id: int = foreign_key(RulesParent, on_delete=OnDelete.RESTRICT)


class RulesUnique(Model, table="rules_unique"):
    id: int = field(primary_key=True)
    parent_id: int = foreign_key(RulesParent, on_delete=OnDelete.RESTRICT, unique=True)


class RulesDerived(Model):
    id: int = field(primary_key=True)


def test_require_explicit_table_flags_derived_names() -> None:
    rules = RuleSet(Models.require_explicit_table(match="tests.rulestest"))
    found = rules.run(project())
    assert [d.rule_id for d in found] == ["HNK-DB-001"]
    assert found[0].message.startswith("RulesDerived uses the derived table name")
    assert found[0].location.startswith("tests/rulestest.py:")
    nothing = RuleSet(Models.require_explicit_table(match="no.such.module")).run(
        project()
    )
    assert [d.status for d in nothing] == [Status.UNVERIFIABLE]


def test_require_foreign_key_index_flags_unindexed_references() -> None:
    rules = RuleSet(Models.require_foreign_key_index(match="tests.rulestest"))
    found = rules.run(project())
    assert [d.rule_id for d in found] == ["HNK-DB-002"]
    assert found[0].message.startswith("RulesLoose.parent_id references RulesParent")
    # foreign_key(...) has no index= option, so the hint must not suggest field().
    assert "field(index=True)" not in found[0].fix
    assert '@db.indexed("parent_id")' in found[0].fix
    nothing = RuleSet(Models.require_foreign_key_index(match="no.such.module")).run(
        project()
    )
    assert [d.status for d in nothing] == [Status.UNVERIFIABLE]


def test_require_atomic_leaves_the_admins_own_routes_alone() -> None:
    from hinoki.admin import Admin  # noqa: PLC0415 - only this test needs the admin

    app = Hinoki(include=[Admin(secret="s" * 40)])
    found = RuleSet(Transactions.require_atomic()).run(Project(app))

    assert failing(found) == []
