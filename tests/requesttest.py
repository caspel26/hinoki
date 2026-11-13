"""The request object: registration rules and what handlers see."""

from typing import Any

import pytest

from hinoki import API, HinokiAPI, RegistrationError, Request, get, post
from hinoki.request import parse_cookies
from tests.harness import Client, Pool


@get("/echo/{name}")
async def echo(request: Request, name: str, tag: str = "") -> dict[str, Any]:
    return {
        "method": request.method,
        "path": request.path,
        "host": request.headers["Host"],
        "agent": request.headers.get("USER-AGENT"),
        "tag": tag,
        "tags": request.GET.getlist("tag"),
        "last": request.GET.get("tag"),
        "cookies": request.COOKIES,
        "user": request.user,
        "params": request.path_params,
        "rid": request.request_id,
    }


@post("/raw")
def raw(request: Request) -> dict[str, Any]:
    return {
        "length": len(request.body),
        "type": request.content_type,
        "data": request.data,
    }


class Things(API, path="/things"):
    @get("/")
    async def listing(self, request: Request) -> dict[str, str]:
        return {"method": request.method}


async def started(pool: Pool) -> Client:
    app = HinokiAPI()
    app.include(echo, raw, Things)
    return await pool.start(app)


async def test_handlers_read_the_request_like_django(pool: Pool) -> None:
    client = await started(pool)
    reply = await client.request(
        "GET",
        "/echo/ada",
        query="tag=a&tag=b",
        headers={"Host": "example.test", "user-agent": "t", "cookie": "s=1; x=2"},
    )
    assert reply.status == 422  # `tag` is bound as a scalar: duplicates are refused
    reply = await client.request(
        "GET",
        "/echo/ada",
        query="other=a&other=b",
        headers={"Host": "example.test", "user-agent": "t", "cookie": "s=1; x=2"},
    )
    body = reply.body
    assert (body["method"], body["path"], body["host"]) == (
        "GET",
        "/echo/ada",
        "example.test",
    )
    assert body["agent"] == "t"
    assert body["cookies"] == {"s": "1", "x": "2"}
    assert body["params"] == {"name": "ada"}
    assert body["user"] is None
    assert body["rid"] == reply.headers["x-request-id"]


async def test_query_values_are_a_list_or_the_last(pool: Pool) -> None:
    client = await started(pool)
    reply = await client.request(
        "GET", "/echo/ada", query="tag=a", headers={"host": "h"}
    )
    assert (reply.body["tags"], reply.body["last"]) == (["a"], "a")


async def test_the_body_is_available_without_a_schema_and_parsed_once(
    pool: Pool,
) -> None:
    client = await started(pool)
    reply = await client.request(
        "POST", "/raw", b'{"a": 1}', content_type="application/json; charset=utf-8"
    )
    assert reply.body == {"length": 8, "type": "application/json", "data": {"a": 1}}
    wrong = await client.request("POST", "/raw", b"a=1", content_type="text/plain")
    assert wrong.status == 415


async def test_api_classes_receive_the_request_after_self(pool: Pool) -> None:
    client = await started(pool)
    assert (await client.request("GET", "/things")).body == {"method": "GET"}


def test_cookie_parsing_is_forgiving_and_first_value_wins() -> None:
    assert parse_cookies('a=1; b="two"; a=3; junk; =x') == {"a": "1", "b": "two"}


@pytest.mark.parametrize(
    "build",
    [
        lambda: get("/a")(lambda: None),
        lambda: get("/b")(lambda name: name),
    ],
)
def test_a_handler_without_the_request_is_refused(build: Any) -> None:
    with pytest.raises(RegistrationError, match="first parameter must be the request"):
        HinokiAPI().include(build())


def test_the_request_must_come_first_and_be_annotated() -> None:
    @get("/late/{x}")
    def late(x: int, request: Request) -> None: ...

    @get("/api")
    def untyped(request: int) -> None: ...

    class Lonely(API, path="/lonely"):
        @get("/")
        def index(self) -> None: ...

    for target in (late, untyped, Lonely):
        with pytest.raises(RegistrationError, match="first parameter"):
            HinokiAPI().include(target)
