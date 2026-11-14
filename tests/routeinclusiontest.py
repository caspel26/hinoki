"""Nested ``include`` lists and the ``routes`` command."""

import json
import types

import pytest

from hinoki import API, Hinoki, Mount, RegistrationError, RouteGroup, http
from hinoki.__main__ import main
from hinoki.request import Request

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


routes = [users, [health, (Billing,)]]


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
