import functools
from collections.abc import Callable
from typing import ParamSpec

import pytest

import hinoki
from hinoki import API, Access, HinokiAPI, RegistrationError, get, post

P = ParamSpec("P")


def test_native_core_is_loaded() -> None:
    assert hinoki.__version__ == "0.0.1"


@get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


editor = Access(auth=object(), scopes=["posts:write"])


class Posts(API, path="/posts"):
    @get("/")
    async def list(self) -> list[int]:
        return []

    @get("/{post_id}")
    def retrieve(self, post_id: int) -> int:
        return post_id

    @post("/{post_id}/publish", status=202, access=editor, transaction=True)
    async def publish(self, post_id: int) -> int:
        return post_id


def make_app() -> HinokiAPI:
    app = HinokiAPI()
    app.include(health, Posts)
    return app


def test_resolves_function_and_class_actions() -> None:
    app = make_app()
    found = app.resolve("GET", "/health")
    assert found is not None and found[0].handler is health

    found = app.resolve("GET", "/posts/7")
    assert found is not None
    route, params = found
    assert params == {"post_id": "7"}
    assert not route.is_async

    found = app.resolve("POST", "/posts/7/publish")
    assert found is not None
    assert found[0].is_async and found[0].transaction and found[0].status == 202
    assert found[0].access is editor
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

    @get("/health")
    def again() -> None: ...

    with pytest.raises(RegistrationError, match="duplicate"):
        app.include(again)
    assert len(app.routes) == before
    found = app.resolve("GET", "/health")
    assert found is not None and found[0].handler is health


def test_include_rejects_undeclared_objects() -> None:
    with pytest.raises(RegistrationError):
        HinokiAPI().include(lambda: None)


def test_decorated_function_keeps_behaviour_and_stacks_with_user_decorators() -> None:
    def twice(fn: Callable[P, int]) -> Callable[P, int]:
        @functools.wraps(fn)
        def inner(*a: P.args, **k: P.kwargs) -> int:
            return fn(*a, **k) * 2

        return inner

    @get("/n")
    @twice
    def n() -> int:
        return 21

    assert n() == 42


def test_invalid_declarations_fail_at_decoration_time() -> None:
    with pytest.raises(ValueError, match="start with"):
        get("no-slash")
    with pytest.raises(ValueError, match="status"):
        get("/x", status=99)
    with pytest.raises(ValueError, match="already declares"):

        @get("/a")
        @get("/b")
        def f() -> None: ...

    with pytest.raises(ValueError, match="path must start"):

        class Bad(API, path="posts"): ...


def test_app_instances_do_not_share_state() -> None:
    first, second = make_app(), HinokiAPI()
    assert first.routes and not second.routes
