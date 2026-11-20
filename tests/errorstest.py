"""HTTP error hierarchy and application error handlers."""

import importlib

import pytest

from hinoki import Hinoki, http
from hinoki.db import UniqueViolation
from hinoki.errors import (
    APIError,
    BadRequest,
    Conflict,
    NotFound,
    ObjectNotFound,
    RegistrationError,
)
from hinoki.http.request import Request
from tests.harness import Pool


class PostAlreadyPublished(Conflict):
    code = "post_already_published"
    message = "The post is already published."


class Quota(Exception): ...


class HardQuota(Quota): ...


class Teapot(APIError):
    status = 418
    code = "teapot"


@http.get("/conflict")
async def conflict(request: Request) -> None:
    raise PostAlreadyPublished()


@http.get("/custom-message")
async def custom_message(request: Request) -> None:
    raise NotFound("No such post.", code="post_missing")


@http.get("/details")
async def details(request: Request) -> None:
    raise BadRequest(details=[{"field": "x"}], headers={"x-hint": "retry"})


@http.get("/quota")
async def quota(request: Request) -> None:
    raise Quota("secret quota internals")


@http.get("/hard-quota")
def hard_quota(request: Request) -> None:
    raise HardQuota()


@http.get("/missing-object")
async def missing_object(request: Request) -> None:
    raise ObjectNotFound("Post not found")


@http.get("/teapot")
async def teapot(request: Request) -> None:
    raise Teapot()


@http.get("/broken-handler")
async def broken_handler(request: Request) -> None:
    raise KeyError("x")


def too_much(error: Quota) -> APIError:
    return Conflict(code="quota_exceeded", message="Quota exceeded.")


async def hard(error: HardQuota) -> APIError:
    return Conflict(code="hard_quota", message="Hard limit.")


def explode(error: KeyError) -> APIError:
    raise RuntimeError("handler failed")


def make_app() -> Hinoki:
    app = Hinoki()
    app.include(conflict, custom_message, details, quota, hard_quota, teapot)
    app.include(broken_handler, missing_object)
    app.errors.handle(Quota)(too_much)
    app.errors.handle(HardQuota)(hard)
    app.errors.handle(KeyError)(explode)
    return app


async def test_domain_subclasses_set_status_code_and_message(pool: Pool) -> None:
    client = await pool.start(make_app())
    reply = await client.request("GET", "/conflict")
    assert reply.status == 409
    assert reply.body["error"] == "post_already_published"
    assert reply.body["message"] == "The post is already published."
    assert reply.body["request_id"] == reply.headers["x-request-id"]


async def test_instances_may_override_message_and_code(pool: Pool) -> None:
    client = await pool.start(make_app())
    reply = await client.request("GET", "/custom-message")
    assert (reply.status, reply.body["error"]) == (404, "post_missing")
    assert reply.body["message"] == "No such post."


async def test_details_and_headers_are_public_data(pool: Pool) -> None:
    client = await pool.start(make_app())
    reply = await client.request("GET", "/details")
    assert reply.status == 400
    assert reply.body["details"] == [{"field": "x"}]
    assert reply.headers["x-hint"] == "retry"


async def test_any_status_can_be_declared(pool: Pool) -> None:
    client = await pool.start(make_app())
    assert (await client.request("GET", "/teapot")).status == 418


async def test_registered_handlers_translate_other_exceptions(pool: Pool) -> None:
    client = await pool.start(make_app())
    reply = await client.request("GET", "/quota")
    assert (reply.status, reply.body["error"]) == (409, "quota_exceeded")
    assert "secret" not in str(reply.body)


async def test_the_most_specific_handler_wins_for_sync_and_async_handlers(
    pool: Pool,
) -> None:
    client = await pool.start(make_app())
    reply = await client.request("GET", "/hard-quota")
    assert reply.body["error"] == "hard_quota"


async def test_a_failing_handler_falls_back_to_a_generic_500(pool: Pool) -> None:
    client = await pool.start(make_app())
    reply = await client.request("GET", "/broken-handler")
    assert (reply.status, reply.body["error"]) == (500, "internal_error")
    assert "handler failed" not in str(reply.body)


def test_registering_the_same_exception_twice_is_an_error() -> None:
    app = make_app()
    with pytest.raises(ValueError, match="already registered"):
        app.errors.handle(Quota)(too_much)


async def test_model_not_found_maps_to_404_without_leaking_the_message(
    pool: Pool,
) -> None:
    client = await pool.start(make_app())
    reply = await client.request("GET", "/missing-object")
    assert (reply.status, reply.body["error"]) == (404, "not_found")
    assert "Post" not in str(reply.body)


def test_errors_live_in_one_namespace_and_not_at_the_package_root() -> None:
    import hinoki  # noqa: PLC0415

    for name in ("NotFound", "Conflict", "APIError", "RegistrationError"):
        assert hasattr(hinoki.errors, name)
        assert not hasattr(hinoki, name)
    assert hinoki.errors.NotFound.status == 404
    with pytest.raises(ImportError):
        importlib.import_module("hinoki.httperrors")


@http.get("/unique")
async def unique(request: Request) -> None:
    raise UniqueViolation("duplicate key value violates constraint posts_title_key")


@http.get("/domain")
async def domain(request: Request) -> None:
    raise PaymentDeclined()


class PaymentDeclined(BadRequest):
    code = "payment_declined"
    message = "The payment was declined."


async def test_a_domain_error_is_answered_without_any_registration(pool: Pool) -> None:
    app = Hinoki()
    app.include(domain)
    reply = await (await pool.start(app)).request("GET", "/domain")
    assert (reply.status, reply.body["error"]) == (400, "payment_declined")


async def test_error_handler_decorates_a_function_and_returns_it(pool: Pool) -> None:
    app = Hinoki()
    app.include(quota)

    @app.error_handler(Quota)
    def translate(error: Quota) -> APIError:
        return APIError("Too much", status=429, code="quota_exceeded")

    assert translate(Quota()).code == "quota_exceeded"
    reply = await (await pool.start(app)).request("GET", "/quota")
    assert (reply.status, reply.body["error"], reply.body["message"]) == (
        429,
        "quota_exceeded",
        "Too much",
    )
    assert "secret" not in str(reply.body)


def test_an_api_error_instance_may_set_its_own_status() -> None:
    assert APIError("x", status=402).status == 402
    assert APIError().status == 500
    assert Conflict("x", status=418).status == 418
    assert Conflict().status == 409


@pytest.mark.parametrize(
    ("kind", "handler", "message"),
    [
        ("quota", too_much, "not an exception class"),
        (Quota, "text", "function of the exception"),
        (Quota, Conflict, "function of the exception"),
    ],
)
def test_error_handler_rejects_unusable_entries(
    kind: object, handler: object, message: str
) -> None:
    app = Hinoki()
    with pytest.raises(TypeError, match=message):
        app.error_handler(kind)(handler)  # type: ignore[type-var,arg-type]
    assert app.errors.handlers == {}


def test_error_registration_is_refused_once_the_app_serves() -> None:
    app = Hinoki()
    app._adapter = object()  # type: ignore[assignment]
    with pytest.raises(RegistrationError, match="frozen"):
        app.error_handler(Quota)


async def test_unique_violations_are_a_409_by_default(pool: Pool) -> None:
    app = Hinoki()
    app.include(unique)
    reply = await (await pool.start(app)).request("GET", "/unique")
    assert (reply.status, reply.body["error"]) == (409, "unique_conflict")
    assert "posts_title_key" not in str(reply.body)


async def test_an_application_handler_replaces_the_unique_default(pool: Pool) -> None:
    app = Hinoki()
    app.include(unique)
    app.error_handler(UniqueViolation)(
        lambda _error: APIError("Taken", status=409, code="already_taken")
    )
    reply = await (await pool.start(app)).request("GET", "/unique")
    assert reply.body["error"] == "already_taken"


def test_error_headers_are_checked_like_response_headers() -> None:
    from hinoki.runtime.responses import public_error

    error = BadRequest(
        headers={
            "x-hint": "retry",
            "x-split": "v\r\nset-cookie: s=1",
            "content-length": "999",
            "transfer-encoding": "chunked",
            "bad name": "x",
        }
    )

    names = [name for name, _ in public_error(error).headers]

    assert names.count(b"content-length") <= 1 and b"transfer-encoding" not in names
    assert b"x-hint" in names
    assert b"x-split" not in names and b"bad name" not in names
