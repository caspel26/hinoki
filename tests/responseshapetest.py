"""The final response headers are shaped once per distinct shape, not per request."""

from typing import Any

import pytest

from hinoki import Hinoki, core, http
from tests.harness import Client, Pool


@http.get("/plain")
async def plain() -> dict[str, int]:
    return {"a": 1}


@http.get("/moved/{name}", status=200)
async def varying(name: str) -> http.Response:
    return http.Response(
        200, '{"n":1}', headers=[("x-name", name)], content_type="application/json"
    )


@pytest.fixture
def shaped(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []
    original = core.response_headers

    def counting(*args: Any) -> Any:
        calls.append(args[0])
        return original(*args)

    monkeypatch.setattr(core, "response_headers", counting)
    return calls


async def serve(pool: Pool) -> Client:
    app = Hinoki()
    app.include(plain, varying)
    return await pool.start(app)


async def test_repeated_requests_of_one_shape_are_shaped_once(
    pool: Pool, shaped: list[Any]
) -> None:
    client = await serve(pool)
    replies = [await client.request("GET", "/plain") for _ in range(5)]
    assert len(shaped) == 1
    ids = {reply.headers["x-request-id"] for reply in replies}
    assert len(ids) == 5
    assert {reply.headers["content-length"] for reply in replies} == {"7"}


async def test_answers_with_their_own_headers_keep_them(pool: Pool) -> None:
    client = await serve(pool)
    first = await client.request("GET", "/moved/a")
    second = await client.request("GET", "/moved/b")
    assert first.headers["x-name"] == "a"
    assert second.headers["x-name"] == "b"
    assert first.headers["x-request-id"] != second.headers["x-request-id"]
