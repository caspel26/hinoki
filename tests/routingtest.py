import functools
from collections.abc import Callable
from typing import cast

import pytest

import hinoki
from hinoki import API, Access, HinokiAPI, RegistrationError, get, post
from hinoki.request import Request
from tests.providers import TokenAuth

editor = Access(auth=TokenAuth(), permissions=["posts:write"])


def twice[**P](fn: Callable[P, int]) -> Callable[P, int]:
    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> int:
        return fn(*args, **kwargs) * 2

    return wrapper


@get("/health")
def health(request: Request) -> dict[str, str]:
    return {"status": "ok"}


@get("/duplicate-health")
def duplicate(request: Request) -> None: ...


@get("/n")
@twice
def answer(request: Request) -> int:
    return 21


class Posts(API, path="/posts"):
    @get("/")
    async def list(self, request: Request) -> list[int]:
        return []

    @get("/{post_id}")
    def retrieve(self, request: Request, post_id: int) -> int:
        return post_id

    @post("/{post_id}/publish", status=202, access=editor, transaction=True)
    async def publish(self, request: Request, post_id: int) -> int:
        return post_id


def make_app() -> HinokiAPI:
    app = HinokiAPI()
    app.include(health, Posts)
    return app


def test_native_core_is_loaded() -> None:
    assert hinoki.__version__ == "0.0.1"


def test_resolves_function_and_class_actions() -> None:
    app = make_app()
    found = app.resolve("GET", "/health")
    assert found is not None
    assert found[0].handler is health

    found = app.resolve("GET", "/posts/7")
    assert found is not None
    route, params = found
    assert params == {"post_id": "7"}
    assert not route.is_async

    found = app.resolve("POST", "/posts/7/publish")
    assert found is not None
    publish = found[0]
    assert publish.is_async
    assert publish.transaction
    assert publish.status == 202
    assert publish.access is editor
    assert editor.scopes == frozenset({"posts:write"})


def test_collection_path_has_no_trailing_slash() -> None:
    assert make_app().resolve("GET", "/posts") is not None


def test_unknown_path_and_wrong_method_do_not_resolve() -> None:
    app = make_app()
    assert app.resolve("GET", "/nope") is None
    assert app.resolve("DELETE", "/posts") is None


def test_duplicate_route_fails_and_leaves_app_unchanged() -> None:
    app = make_app()
    before = len(app.routes)
    clash = get("/health")(lambda: None)
    with pytest.raises(RegistrationError, match="duplicate"):
        app.include(clash)
    assert len(app.routes) == before
    found = app.resolve("GET", "/health")
    assert found is not None
    assert found[0].handler is health


def test_include_rejects_undeclared_objects() -> None:
    with pytest.raises(RegistrationError):
        HinokiAPI().include(lambda: None)


def test_user_decorators_keep_behaviour_under_a_route() -> None:
    assert answer(cast(Request, None)) == 42


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: get("no-slash"), "start with"),
        (lambda: get("/x", status=99), "status"),
        (lambda: get("/a")(get("/b")(lambda: None)), "already declares"),
    ],
)
def test_invalid_declarations_fail_at_decoration_time(
    build: Callable[[], object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build()


def test_api_prefix_must_be_well_formed() -> None:
    with pytest.raises(ValueError, match="path must start"):
        type("Bad", (API,), {}, path="posts")


def test_app_instances_do_not_share_state() -> None:
    first, second = make_app(), HinokiAPI()
    assert first.routes
    assert not second.routes
