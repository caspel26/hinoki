"""HTTP error hierarchy and application error handlers."""

import pytest

from hinoki import APIError, BadRequest, Conflict, HinokiAPI, NotFound, get
from hinoki.errors import ObjectNotFound
from tests.harness import Pool


class PostAlreadyPublished(Conflict):
    code = "post_already_published"
    message = "The post is already published."


class Quota(Exception): ...


class HardQuota(Quota): ...


class Teapot(APIError):
    status = 418
    code = "teapot"


@get("/conflict")
async def conflict() -> None:
    raise PostAlreadyPublished()


@get("/custom-message")
async def custom_message() -> None:
    raise NotFound("No such post.", code="post_missing")


@get("/details")
async def details() -> None:
    raise BadRequest(details=[{"field": "x"}], headers={"x-hint": "retry"})


@get("/quota")
async def quota() -> None:
    raise Quota("secret quota internals")


@get("/hard-quota")
def hard_quota() -> None:
    raise HardQuota()


@get("/missing-object")
async def missing_object() -> None:
    raise ObjectNotFound("Post not found")


@get("/teapot")
async def teapot() -> None:
    raise Teapot()


@get("/broken-handler")
async def broken_handler() -> None:
    raise KeyError("x")


def too_much(error: Quota) -> APIError:
    return Conflict(code="quota_exceeded", message="Quota exceeded.")


async def hard(error: HardQuota) -> APIError:
    return Conflict(code="hard_quota", message="Hard limit.")


def explode(error: KeyError) -> APIError:
    raise RuntimeError("handler failed")


def make_app() -> HinokiAPI:
    app = HinokiAPI()
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
