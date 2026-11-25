"""Nested ``include`` lists and the ``routes`` command."""

import json
import re
import types

import pytest

from hinoki import API, Hinoki, Includable, Mount, RouteGroup, http
from hinoki.__main__ import main
from hinoki.errors import RegistrationError
from hinoki.http.request import Request
from hinoki.rules import qualified
from hinoki.terminal import Style

ANSI = re.compile(r"\x1b\[[0-9;]*m")
APP = "tests.routeinclusiontest:make_app"

users = RouteGroup("/users")


@users.get("/")
def listing() -> list[int]:
    return []


@http.get("/health")
def health(request: Request) -> dict[str, str]:
    return {"status": "ok"}


class Billing(API, path="/billing"):
    @http.post("/", status=201)
    def create(self, request: Request) -> None: ...


routes: list[Includable] = [users, [health, (Billing,)]]


def make_app() -> Hinoki:
    return Hinoki(include=[routes])


def test_nested_lists_and_tuples_are_spread_in_order() -> None:
    app = make_app()
    assert [(r.method, r.path) for r in app.routes] == [
        ("GET", "/users"),
        ("GET", "/health"),
        ("POST", "/billing"),
    ]


def test_a_group_reached_twice_through_nesting_is_refused() -> None:
    with pytest.raises(RegistrationError, match="only once"):
        Hinoki(include=[[users], [users]])


def test_a_module_is_refused_with_a_hint() -> None:
    with pytest.raises(RegistrationError, match="export a list"):
        Hinoki(include=[types.ModuleType("feature")])


def test_routes_command_lists_paths_in_order(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["routes", APP]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[:2] for line in lines] == [
        ["POST", "/billing"],
        ["GET", "/health"],
        ["GET", "/users"],
    ]
    assert "public" in lines[0]


def test_routes_command_aligns_handlers_past_a_long_access(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "hinoki.cli.routes.access_of",
        lambda route: (
            "LongTokens[tracker:use-it]" if route.path == "/health" else "public"
        ),
    )
    assert main(["routes", APP]) == 0
    lines = capsys.readouterr().out.splitlines()
    starts = {line.index("tests.") for line in lines}
    assert len(starts) == 1


def test_routes_command_aligns_handlers_in_the_colored_output(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "hinoki.cli.routes.access_of",
        lambda route: (
            "LongTokens[tracker:use-it]" if route.path == "/health" else "public"
        ),
    )
    monkeypatch.setattr(
        "hinoki.cli.routes.Style.detect", lambda *_args: Style(color=True)
    )
    assert main(["routes", APP]) == 0
    plain = [ANSI.sub("", line) for line in capsys.readouterr().out.splitlines()]
    assert len({line.index("tests.") for line in plain}) == 1


def test_routes_command_filters_and_prints_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["routes", APP, "--path", "/u", "--format", "json"]) == 0
    found = json.loads(capsys.readouterr().out)
    assert [(r["method"], r["path"], r["access"]) for r in found] == [
        ("GET", "/users", "public")
    ]
    assert found[0]["handler"].endswith("routeinclusiontest.listing")
    assert main(["routes", APP, "--method", "delete"]) == 1
    assert main(["routes", "no.such.module:app"]) == 2


class Shared(API):
    @http.get("/")
    def listing(self) -> None: ...


class Notes(Shared, path="/notes"):
    pass


class Tags(Shared, path="/tags"):
    pass


def make_inherited() -> Hinoki:
    return Hinoki(include=[Notes, Tags])


def test_routes_names_handlers_as_rules_and_waivers_do(
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = "tests.routeinclusiontest:make_inherited"
    assert main(["routes", target, "--format", "json"]) == 0
    found = json.loads(capsys.readouterr().out)
    assert [r["handler"] for r in found] == [
        "tests.routeinclusiontest.Notes.listing",
        "tests.routeinclusiontest.Tags.listing",
    ]
    assert [r["handler"] for r in found] == [
        qualified(r) for r in make_inherited().routes
    ]


def test_mount_places_a_feature_under_a_prefix_chosen_at_the_root() -> None:
    root = RouteGroup()

    @root.get("/")
    def home() -> None: ...

    app = Hinoki(include=[Mount("/api", Mount("/v1", [Billing, root]), health)])
    assert sorted((r.method, r.path) for r in app.routes) == [
        ("GET", "/api/health"),
        ("GET", "/api/v1"),
        ("POST", "/api/v1/billing"),
    ]
    assert app.resolve("POST", "/api/v1/billing") is not None
    assert app.resolve("POST", "/billing") is None


def test_mount_validates_prefix_and_content() -> None:
    for bad in ("api", "/api/", ""):
        with pytest.raises(ValueError, match="mount prefix"):
            Mount(bad, health)
    with pytest.raises(ValueError, match="at least one"):
        Mount("/api")


def test_mounted_routes_can_still_collide() -> None:
    with pytest.raises(RegistrationError, match="duplicate"):
        Hinoki(include=[Mount("/x", health), Mount("/x", health)])
