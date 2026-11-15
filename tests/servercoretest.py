"""The native request classifier against the Python pipeline (no sockets)."""

import json
import re
from typing import Any

import pytest

from hinoki import Hinoki, RuntimeConfig, core, http
from hinoki.request import Request
from hinoki.runtime.servercore import SENTINEL, build_server_core, early_bodies
from tests.harness import Client, Pool

HEX32 = re.compile(r"[0-9a-f]{32}")


@http.get("/ping")
def ping(request: Request) -> dict[str, str]:
    return {"pong": "yes"}


@http.get("/items/{item_id}")
async def item(request: Request, item_id: int) -> dict[str, int]:
    return {"id": item_id}


@http.post("/made", status=201)
async def made(request: Request) -> dict[str, bool]:
    return {"ok": True}


def make_app(**config: int) -> Hinoki:
    app = Hinoki(runtime=RuntimeConfig(**config))
    app.include(ping, item, made)
    return app


def classifier(app: Hinoki, ready: bool = True) -> core.ServerCore:
    server = build_server_core(app.asgi().pipeline)
    server.set_ready(ready)
    return server


def body_of(raw: Any, request_id: str) -> Any:
    """The decoded JSON body with the request id replaced, so answers compare."""
    decoded = json.loads(raw) if isinstance(raw, bytes) else raw
    if decoded is not None:
        assert decoded["request_id"] == request_id
        decoded["request_id"] = "<id>"
    return decoded


def normalized(status: int, headers: list[tuple[bytes, bytes]], body: bytes) -> Any:
    """A native answer in the shape of ``expected``."""
    pairs = [(k.decode(), v.decode()) for k, v in headers]
    request_id = dict(pairs)["x-request-id"]
    assert HEX32.fullmatch(request_id)
    pairs = [(k, "<id>" if k == "x-request-id" else v) for k, v in pairs]
    return status, pairs, body_of(body, request_id) if body else None


def expected(reply: Any) -> Any:
    pairs = list(reply.headers.items())  # dicts keep the order they were sent in
    request_id = reply.headers["x-request-id"]
    pairs = [(k, "<id>" if k == "x-request-id" else v) for k, v in pairs]
    return reply.status, pairs, body_of(reply.body, request_id)


EARLY_CASES: list[tuple[str, str, str, list[tuple[bytes, bytes]]]] = [
    ("GET", "/nope", "", []),
    ("GET", "/made", "", []),
    ("POST", "/ping", "", []),
    ("DELETE", "/items/3", "", []),
    ("GET", "/ping", "q=" + "x" * 9000, []),
    ("GET", "/ping", "", [(b"x-big", b"y" * 40_000)]),
]


@pytest.mark.parametrize(("method", "path", "query", "headers"), EARLY_CASES)
async def test_early_answers_match_the_python_pipeline(
    pool: Pool,
    method: str,
    path: str,
    query: str,
    headers: list[tuple[bytes, bytes]],
) -> None:
    app = make_app()
    client = await pool.start(app)
    reply = await client.request(method, path, query=query, raw_headers=headers or None)
    submission = classifier(app).submit(method, path, query.encode(), headers)
    assert submission.early is not None
    assert normalized(*submission.early) == expected(reply)


async def test_not_ready_matches_the_python_pipeline() -> None:
    app = make_app()
    reply = await Client(app.asgi()).request("GET", "/ping")
    submission = classifier(app, ready=False).submit("GET", "/ping", b"", [])
    assert submission.early is not None
    assert (reply.status, reply.headers["retry-after"]) == (503, "1")
    assert normalized(*submission.early) == expected(reply)


def test_head_of_an_unknown_path_keeps_the_length_and_drops_the_body() -> None:
    server = classifier(make_app())
    get = server.submit("GET", "/nope", b"", []).early
    head = server.submit("HEAD", "/nope", b"", []).early
    assert get is not None
    assert head is not None
    assert head[2] == b""
    lengths = [dict(h)[b"content-length"] for h in (get[1], head[1])]
    assert lengths[0] == lengths[1]


def test_a_routed_request_is_queued_with_its_facts() -> None:
    server = classifier(make_app())
    submission = server.submit("GET", "/items/42", b"a=1", [(b"host", b"h")])
    assert submission.early is None
    assert submission.wake is True
    [request] = server.drain(10)
    assert (request.method, request.path, request.query) == ("GET", "/items/42", b"a=1")
    assert request.params == {"item_id": "42"}
    assert request.action is not None
    assert HEX32.fullmatch(request.request_id)
    assert request.headers == [(b"host", b"h")]
    assert request.needs_headers is True  # the handler takes a Request


def test_only_the_first_queued_request_asks_for_a_wake() -> None:
    server = classifier(make_app())
    wakes = [server.submit("GET", "/ping", b"", []).wake for _ in range(3)]
    assert wakes == [True, False, False]
    assert len(server.drain(2)) == 2
    assert server.pending == 1


def test_drain_keeps_arrival_order() -> None:
    server = classifier(make_app())
    for number in range(4):
        server.submit("GET", f"/items/{number}", b"", [])
    ids = [r.params["item_id"] for r in server.drain(10)]
    assert ids == ["0", "1", "2", "3"]


def test_head_borrows_the_get_route() -> None:
    server = classifier(make_app())
    server.submit("GET", "/ping", b"", [])
    server.submit("HEAD", "/ping", b"", [])
    get, head = server.drain(2)
    assert head.action == get.action
    assert head.method == "HEAD"


def test_respond_reaches_the_submission_once() -> None:
    server = classifier(make_app())
    submission = server.submit("GET", "/ping", b"", [])
    [request] = server.drain(1)
    assert submission.reply() is None
    assert request.respond(200, [(b"x-a", b"1")], b"body") is True
    assert submission.reply() == (200, [(b"x-a", b"1")], b"body")
    assert request.respond(500, [], b"") is False


def test_respond_after_the_client_left_returns_false() -> None:
    server = classifier(make_app())
    submission = server.submit("GET", "/ping", b"", [])
    [request] = server.drain(1)
    submission.cancel()
    assert request.respond(200, [], b"") is False


def test_in_flight_bound_answers_503_and_a_dropped_request_frees_its_slot() -> None:
    app = make_app(sync_workers=1, sync_queue=0)  # bound = 4 * (1 + 0)
    server = classifier(app)
    for _ in range(4):
        assert server.submit("GET", "/ping", b"", []).early is None
    refused = server.submit("GET", "/ping", b"", []).early
    assert refused is not None
    assert refused[0] == 503
    assert b"overloaded" in refused[2]
    assert server.in_flight == 4
    queued = server.drain(10)
    del queued
    assert server.in_flight == 0
    assert server.submit("GET", "/ping", b"", []).early is None


def test_docs_and_openapi_paths_go_to_python() -> None:
    app = Hinoki(openapi_url="/openapi.json", docs_url="/docs")
    app.include(ping)
    server = classifier(app)
    for path in ("/openapi.json", "/docs"):
        submission = server.submit("GET", path, b"", [])
        assert submission.early is None
    assert [r.action for r in server.drain(10)] == [None, None]


def test_configured_policies_send_unrouted_requests_to_python() -> None:
    server = core.ServerCore(
        [],
        early_bodies(),
        [],
        b"x-request-id",
        "0123456789abcdef",
        defer_policies=True,
    )
    server.set_ready(True)
    assert server.submit("GET", "/anything", b"", []).early is None
    [request] = server.drain(1)
    assert request.action is None
    assert request.needs_headers is True


def test_a_body_without_one_sentinel_is_refused() -> None:
    bad = [(kind, b"{}") for kind, _ in early_bodies()]
    with pytest.raises(ValueError, match="sentinels"):
        core.ServerCore([], bad, [], b"x-request-id", "0123456789abcdef")


def test_bodies_are_rendered_with_the_sentinel() -> None:
    for kind, body in early_bodies():
        assert body.count(SENTINEL.encode()) == 1, kind
