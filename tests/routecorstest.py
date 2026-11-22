"""Per-route CORS: ``cors=http.Cors(...)`` on an action, a group or ``crud(...)``."""

from typing import Any
from uuid import UUID, uuid4

import pytest

from hinoki import Hinoki, Schema, crud, http
from hinoki.orm import Model, field
from hinoki.routing import RouteGroup
from hinoki.runtime.servercore import SENTINEL
from hinoki.settings import AppSettings, CorsSettings
from tests.harness import Pool
from tests.servercoretest import (
    EmptyBody,
    classifier,
    final_view,
    native_final,
    scope_of,
)

GLOBAL = "https://app.example"
OWN = "https://partner.example"
OTHER = "https://other.example"
PARTNER = http.Cors(origins=(OWN,), credentials=True, methods=("GET",), max_age=60)


@http.get("/shared")
def shared() -> dict[str, str]:
    return {"scope": "app"}


@http.get("/partner", cors=PARTNER)
async def partner() -> dict[str, str]:
    return {"scope": "route"}


@http.post("/partner", cors=PARTNER)
async def partner_write() -> dict[str, str]:
    return {"scope": "write"}


feed = RouteGroup("/feed", cors=http.Cors(origins=("*",)))


@feed.get("/")
def feed_list() -> dict[str, str]:
    return {"scope": "group"}


@feed.get("/own", cors=None)
def feed_own() -> dict[str, str]:
    return {"scope": "app"}


def make_app(*, global_cors: bool = True) -> Hinoki:
    settings = AppSettings(cors=CorsSettings(origins=(GLOBAL,) if global_cors else ()))
    app = Hinoki(settings=settings)
    app.include(shared, partner, partner_write, feed)
    return app


async def started(pool: Pool, *, global_cors: bool = True) -> Hinoki:
    app = make_app(global_cors=global_cors)
    await pool.start(app)
    return app


def preflight(method: str, origin: str) -> list[Any]:
    return [
        (b"origin", origin.encode()),
        (b"access-control-request-method", method.encode()),
    ]


async def answer(
    app: Hinoki, method: str, path: str, headers: list[tuple[bytes, bytes]]
) -> tuple[int, dict[str, str]]:
    response = await app.asgi().pipeline.handle(
        scope_of(method, path, "", headers, "http"), EmptyBody(), SENTINEL
    )
    return response.status, {
        k.decode(): v.decode() for k, v in response.headers if k != b"vary"
    } | {
        "vary": ",".join(v.decode() for k, v in response.headers if k == b"vary"),
    }


async def test_a_route_policy_replaces_the_application_policy(pool: Pool) -> None:
    app = await started(pool)
    status, own = await answer(app, "GET", "/partner", [(b"origin", OWN.encode())])
    assert status == 200
    assert own["access-control-allow-origin"] == OWN
    assert own["access-control-allow-credentials"] == "true"
    assert own["vary"] == "origin"

    # The application's origin is not the route's: no permission, still varies.
    _, denied = await answer(app, "GET", "/partner", [(b"origin", GLOBAL.encode())])
    assert "access-control-allow-origin" not in denied
    assert denied["vary"] == "origin"

    # A route without cors= keeps the application's policy.
    _, shared_headers = await answer(
        app, "GET", "/shared", [(b"origin", GLOBAL.encode())]
    )
    assert shared_headers["access-control-allow-origin"] == GLOBAL
    assert "access-control-allow-credentials" not in shared_headers


async def test_a_route_policy_works_without_an_application_policy(pool: Pool) -> None:
    app = await started(pool, global_cors=False)
    _, own = await answer(app, "GET", "/partner", [(b"origin", OWN.encode())])
    assert own["access-control-allow-origin"] == OWN
    _, plain = await answer(app, "GET", "/shared", [(b"origin", OWN.encode())])
    assert "access-control-allow-origin" not in plain
    status, _ = await answer(app, "OPTIONS", "/shared", preflight("GET", OWN))
    assert status == 405


async def test_errors_after_routing_carry_the_route_policy(pool: Pool) -> None:
    app = await started(pool)
    _, wrong_method = await answer(app, "PUT", "/partner", [(b"origin", OWN.encode())])
    assert "access-control-allow-origin" not in wrong_method
    _, bad_path = await answer(app, "GET", "/missing", [(b"origin", GLOBAL.encode())])
    assert bad_path["access-control-allow-origin"] == GLOBAL


@pytest.mark.parametrize(
    ("method", "path", "origin", "status", "allow_methods"),
    [
        ("GET", "/partner", OWN, 204, "GET"),
        ("GET", "/partner", GLOBAL, 403, None),
        ("POST", "/partner", OWN, 204, "GET"),
        ("GET", "/shared", GLOBAL, 204, "GET, POST, PUT, PATCH, DELETE, QUERY"),
        ("GET", "/shared", OWN, 403, None),
        # A method no route has on that path answers with the application's policy.
        ("DELETE", "/partner", GLOBAL, 204, "GET, POST, PUT, PATCH, DELETE, QUERY"),
        ("GET", "/feed", OTHER, 204, "GET, POST, PUT, PATCH, DELETE, QUERY"),
        ("GET", "/feed/own", GLOBAL, 204, "GET, POST, PUT, PATCH, DELETE, QUERY"),
        ("GET", "/feed/own", OTHER, 403, None),
    ],
)
async def test_a_preflight_is_decided_by_the_route_it_asks_about(
    pool: Pool,
    method: str,
    path: str,
    origin: str,
    status: int,
    allow_methods: str | None,
) -> None:
    app = await started(pool)
    headers = preflight(method, origin)
    reply_status, reply = await answer(app, "OPTIONS", path, headers)
    assert reply_status == status
    if allow_methods is not None:
        assert reply["access-control-allow-methods"] == allow_methods
        assert reply["access-control-allow-origin"] == (
            "*" if path == "/feed" else origin
        )


async def test_a_route_preflight_carries_its_own_max_age_and_credentials(
    pool: Pool,
) -> None:
    app = await started(pool)
    _, reply = await answer(app, "OPTIONS", "/partner", preflight("GET", OWN))
    assert reply["access-control-max-age"] == "60"
    assert reply["access-control-allow-credentials"] == "true"
    assert reply["vary"].startswith("access-control-request-method")


async def test_a_group_policy_is_the_default_and_none_restores_the_app_policy(
    pool: Pool,
) -> None:
    app = await started(pool)
    _, group = await answer(app, "GET", "/feed", [(b"origin", OTHER.encode())])
    assert group["access-control-allow-origin"] == "*"
    _, own = await answer(app, "GET", "/feed/own", [(b"origin", GLOBAL.encode())])
    assert own["access-control-allow-origin"] == GLOBAL


@pytest.mark.parametrize(
    ("method", "path", "origin"),
    [
        ("OPTIONS", "/partner", OWN),
        ("OPTIONS", "/partner", GLOBAL),
        ("OPTIONS", "/shared", GLOBAL),
        ("OPTIONS", "/shared", OWN),
        ("OPTIONS", "/feed", ""),
        ("OPTIONS", "/partner", ""),
        ("GET", "/partner", OWN),
        ("GET", "/partner", GLOBAL),
        ("GET", "/shared", GLOBAL),
        ("GET", "/feed", OTHER),
    ],
)
async def test_the_native_classifier_agrees_with_the_pipeline(
    pool: Pool, method: str, path: str, origin: str
) -> None:
    app = await started(pool)
    headers = [(b"origin", origin.encode())] if origin else []
    if method == "OPTIONS":
        headers.append((b"access-control-request-method", b"GET"))
    server = classifier(app)
    submission = server.submit(method, path, b"", headers)
    response = await app.asgi().pipeline.handle(
        scope_of(method, path, "", headers, "http"), EmptyBody(), SENTINEL
    )
    if submission.early is None:
        submission_answer = await native_final(app, server, submission)
    else:
        submission_answer = submission.early
    assert final_view(*submission_answer) == final_view(
        response.status, response.headers, response.body
    )


def test_a_policy_names_origins() -> None:
    with pytest.raises(ValueError, match="at least one origin"):
        http.Cors()
    with pytest.raises(ValueError, match="cannot be combined with credentials"):
        http.Cors(origins=("*",), credentials=True)


def test_cors_takes_an_http_cors() -> None:
    with pytest.raises(ValueError, match=r"takes an http\.Cors"):
        http.get("/x", cors=CorsSettings(origins=(OWN,)))(lambda: None)  # type: ignore[arg-type]


class Note(Model, table="route_cors_notes"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str = field(max_length=40)


class NoteOut(Schema):
    id: UUID
    title: str


class NoteIn(Schema):
    title: str


def test_generated_crud_actions_carry_the_policy() -> None:
    app = Hinoki()
    app.include(
        crud(
            Note,
            path="/notes",
            read=NoteOut,
            create=NoteIn,
            delete=True,
            public_writes=True,
            cors=PARTNER,
        )
    )
    assert len(app.routes) == 4
    assert all(route.cors is PARTNER for route in app.routes)
