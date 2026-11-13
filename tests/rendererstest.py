"""Renderers and Accept negotiation."""

from typing import Any, cast

import pytest
from openapi_spec_validator import validate

from hinoki import HinokiAPI, JSONRenderer, Renderer, Request, get
from tests.harness import Client, Pool


class CSVRenderer(Renderer):
    media_type = "text/csv"

    def render(self, value: object, request: Request) -> bytes:
        rows = cast(list[dict[str, Any]], value if isinstance(value, list) else [value])
        lines = (",".join(str(cell) for cell in row.values()) for row in rows)
        return "\n".join(lines).encode()


@get("/default")
def default(request: Request) -> dict[str, int]:
    return {"a": 1}


@get("/many", renderers=[JSONRenderer(), CSVRenderer()])
def many(request: Request) -> list[dict[str, Any]]:
    return [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]


@get("/csvonly", renderers=[CSVRenderer()])
def csvonly(request: Request) -> dict[str, int]:
    return {"a": 1}


@get("/nothing", renderers=[CSVRenderer()])
def nothing(request: Request) -> None: ...


def app() -> HinokiAPI:
    result = HinokiAPI(openapi_url="/openapi.json")
    result.include(default, many, csvonly, nothing)
    return result


async def fetch(client: Client, path: str, accept: str | None = None) -> Any:
    headers = {"accept": accept} if accept is not None else None
    return await client.request("GET", path, headers=headers)


async def test_json_is_the_default_whatever_is_accepted_first(pool: Pool) -> None:
    client = await pool.start(app())
    for accept in (None, "*/*", "application/json", "application/*, text/html;q=0.1"):
        reply = await fetch(client, "/default", accept)
        assert (reply.status, reply.body) == (200, {"a": 1})
        assert reply.headers["content-type"] == "application/json"
        assert "vary" not in reply.headers


async def test_the_accept_header_picks_among_several_renderers(pool: Pool) -> None:
    client = await pool.start(app())
    asked_json = await fetch(client, "/many", "application/json")
    assert asked_json.body[0] == {"a": 1, "b": "x"}
    assert asked_json.headers["vary"] == "accept"
    prefers_csv = await client.request(
        "GET", "/many", headers={"accept": "text/csv, application/json;q=0.5"}
    )
    assert prefers_csv.headers["content-type"] == "text/csv"
    assert prefers_csv.headers["vary"] == "accept"
    first = await fetch(client, "/many")
    assert first.headers["content-type"] == "application/json"


async def test_an_unacceptable_request_is_refused_before_the_handler(
    pool: Pool,
) -> None:
    client = await pool.start(app())
    for path, accept in (
        ("/default", "text/html"),
        ("/csvonly", "application/json"),
        ("/many", "application/json;q=0, text/csv;q=0"),
    ):
        reply = await fetch(client, path, accept)
        assert (reply.status, reply.body["error"]) == (406, "not_acceptable")
    assert "available" in reply.body


async def test_errors_stay_json_and_empty_answers_have_no_body(pool: Pool) -> None:
    client = await pool.start(app())
    missing = await fetch(client, "/missing", "text/csv")
    assert (missing.status, missing.headers["content-type"]) == (
        404,
        "application/json",
    )
    empty = await fetch(client, "/nothing", "text/csv")
    assert (empty.status, empty.body) == (204, None)


def test_openapi_lists_the_media_types_of_the_renderers() -> None:
    document = app().openapi()
    validate(document)
    content = document["paths"]["/many"]["get"]["responses"]["200"]["content"]
    assert set(content) == {"application/json", "text/csv"}


@pytest.mark.parametrize(
    "renderers", [[], [JSONRenderer(), JSONRenderer()], [Renderer()]]
)
def test_renderer_lists_are_checked_at_declaration(renderers: list[Renderer]) -> None:
    with pytest.raises(ValueError, match="renderers must be"):
        get("/x", renderers=renderers)(len)
