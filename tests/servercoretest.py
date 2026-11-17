"""The native request classifier against the Python pipeline (no sockets)."""

import json
import re
from typing import Any

import pytest

from hinoki import Hinoki, RuntimeConfig, core, http
from hinoki.http.request import Request
from hinoki.renderers import JSONRenderer
from hinoki.runtime.servercore import SENTINEL, build_server_core, early_bodies
from hinoki.settings import AppSettings, CorsSettings, SecuritySettings, ServerSettings
from tests.harness import Client, Pool

HEX32 = re.compile(r"[0-9a-f]{32}")


class EmptyBody:
    async def read(self, limit: int) -> bytes:
        return b""


class StringSubclass(str):
    pass


@http.get("/ping")
def ping(request: Request) -> dict[str, str]:
    return {"pong": "yes"}


@http.get("/native")
async def native() -> dict[str, str]:
    return {"native": "yes"}


@http.get("/native/items/{item_id}")
def native_item(item_id: str) -> dict[str, str]:
    return {"item_id": item_id}


@http.get("/native/search")
def native_search(term: str) -> dict[str, str]:
    return {"term": term}


@http.get("/native/numbers/{item_id}")
def native_number(item_id: int) -> dict[str, int]:
    return {"item_id": item_id}


@http.get("/native/default")
def native_default(term: str = "hinoki") -> dict[str, str]:
    return {"term": term}


@http.get("/native/default-path/{item_id}")
def native_default_path(item_id: str = "fallback") -> dict[str, str]:
    return {"item_id": item_id}


@http.get("/native/default-number")
def native_default_number(value: int = 4) -> dict[str, int]:
    return {"value": value}


@http.get("/native/default-subclass")
def native_default_subclass(value: str = StringSubclass("custom")) -> dict[str, str]:
    return {"value": value}


@http.get("/custom-renderer", renderers=[JSONRenderer(indent=2)])
async def custom_renderer() -> dict[str, str]:
    return {"native": "no"}


@http.get("/items/{item_id}")
async def item(request: Request, item_id: int) -> dict[str, int]:
    return {"id": item_id}


@http.get("/strings/{item_id}")
async def string_item(request: Request, item_id: str) -> dict[str, str]:
    return {"id": item_id}


@http.post("/made", status=201)
async def made(request: Request) -> dict[str, bool]:
    return {"ok": True}


def make_app(**config: int) -> Hinoki:
    app = Hinoki(runtime=RuntimeConfig(**config))
    app.include(
        ping,
        native,
        native_item,
        native_search,
        native_number,
        native_default,
        native_default_path,
        native_default_number,
        native_default_subclass,
        custom_renderer,
        item,
        string_item,
        made,
    )
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


def test_python_routes_receive_prepared_path_values_or_a_deferred_error() -> None:
    server = classifier(make_app())
    server.submit("GET", "/items/%34", b"", [])
    server.submit("GET", "/strings/a%2Fb", b"", [])
    server.submit("GET", "/strings/%FF", b"", [])
    number, encoded, invalid = server.drain(3)

    assert number.params == {"item_id": "%34"}
    assert number.prepared_path_params == {"item_id": "4"}
    assert encoded.params == {"item_id": "a%2Fb"}
    assert encoded.prepared_path_params == {"item_id": "a/b"}
    assert invalid.prepared_path_params is None


def test_native_dispatch_selects_only_supported_get_inputs() -> None:
    server = classifier(make_app())
    server.submit("GET", "/native", b"", [])
    server.submit("GET", "/native/items/a%2Fb", b"", [])
    server.submit("GET", "/native/search", b"term=green+tea", [])
    server.submit("GET", "/native/numbers/4", b"", [])
    server.submit("GET", "/native/default", b"", [])
    server.submit("GET", "/native/default-path/actual", b"", [])
    server.submit("GET", "/native/default-number", b"", [])
    server.submit("GET", "/native/default-subclass", b"", [])
    server.submit("GET", "/ping", b"", [])
    server.submit("GET", "/custom-renderer", b"", [])
    (
        native,
        path,
        query,
        number,
        default,
        default_path,
        default_number,
        default_subclass,
        request_handler,
        custom,
    ) = server.drain(10)
    assert native.native_handler is True
    assert path.native_handler is True
    assert path.native_args == {"item_id": "a/b"}
    assert query.native_handler is True
    assert query.native_args == {"term": "green tea"}
    assert number.native_handler is False
    assert default.native_handler is True
    assert default.native_args == {}
    assert default_path.native_handler is False
    assert default_number.native_handler is False
    assert default_subclass.native_handler is False
    assert request_handler.native_handler is False
    assert custom.native_handler is False


def test_native_query_default_is_omitted_until_python_restores_the_same_object() -> (
    None
):
    app = Hinoki(include=[native_default])
    server = classifier(app)
    missing = server.submit("GET", "/native/default", b"", [])
    supplied = server.submit("GET", "/native/default", b"term=custom", [])
    [missing_request, supplied_request] = server.drain(2)
    assert missing.wake is True
    assert supplied.wake is False
    assert missing_request.native_handler is True
    assert missing_request.native_args == {}
    assert supplied_request.native_handler is True
    assert supplied_request.native_args == {"term": "custom"}


@pytest.mark.parametrize("inputs", [[], [("path", "other", False)]])
def test_native_plan_rejects_inputs_that_do_not_match_the_template(
    inputs: list[tuple[str, str, bool]],
) -> None:
    route: core.RouteSpec = (
        0,
        "GET",
        "/items/{item}",
        False,
        False,
        ["application/json"],
        200,
        [],
        False,
        False,
        (True, inputs),
        None,
    )
    with pytest.raises(ValueError, match="InvalidNativeHandler"):
        core.ServerCore([route], early_bodies(), ([], []), b"x-request-id", "0" * 16)


def test_native_response_finalization_uses_the_https_policy() -> None:
    app = Hinoki(
        settings=AppSettings(security=SecuritySettings(hsts_seconds=31_536_000))
    )
    app.include(native)
    server = classifier(app)
    submission = server.submit("GET", "/native", b"", [], https=True)
    [request] = server.drain(1)
    assert request.respond_final(
        200, [(b"content-type", b"application/json")], b'{"native":"yes"}'
    )
    reply = submission.reply()
    assert reply is not None
    assert dict(reply[1])[b"strict-transport-security"] == b"max-age=31536000"


def test_startup_probe_answers_get_head_and_refuses_bad_requests_natively() -> None:
    server = classifier(Hinoki(startup_url="/health"))

    get = server.submit("GET", "/health", b"", []).early
    assert get is not None
    assert get[0] == 200
    assert json.loads(get[2]) == {"status": "ok"}
    assert (b"content-type", b"application/json") in get[1]
    assert (b"content-length", str(len(get[2])).encode()) in get[1]

    head = server.submit("HEAD", "/health", b"", []).early
    assert head is not None
    assert head[0] == 200
    assert head[2] == b""
    assert dict(head[1])[b"content-length"] == str(len(get[2])).encode()

    wrong_method = server.submit("POST", "/health", b"", []).early
    assert wrong_method is not None and wrong_method[0] == 405
    assert dict(wrong_method[1])[b"allow"] == b"GET, HEAD"

    unacceptable = server.submit(
        "GET", "/health", b"", [(b"accept", b"text/plain")]
    ).early
    assert unacceptable is not None and unacceptable[0] == 406
    assert json.loads(unacceptable[2])["available"] == "application/json"

    malformed = server.submit("GET", "/health", b"x=%FF", []).early
    assert malformed is not None and malformed[0] == 400
    assert json.loads(malformed[2])["error"] == "invalid_query"
    assert server.pending == 0


def test_startup_probe_obeys_readiness_before_success() -> None:
    server = classifier(Hinoki(startup_url="/health"), ready=False)
    not_ready = server.submit("GET", "/health", b"", []).early
    assert not_ready is not None and not_ready[0] == 503
    assert json.loads(not_ready[2])["error"] == "not_ready"
    server.set_ready(True)
    ready = server.submit("GET", "/health", b"", []).early
    assert ready is not None and ready[0] == 200


def test_startup_probe_is_answered_after_native_host_policy() -> None:
    app = Hinoki(
        startup_url="/health",
        settings=AppSettings(allowed_hosts=("example.com",)),
    )
    server = classifier(app)
    submission = server.submit("GET", "/health", b"", [(b"host", b"example.com")])
    assert submission.early is not None
    assert submission.early[0] == 200
    assert json.loads(submission.early[2]) == {"status": "ok"}


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


def test_native_policies_run_before_unrouted_python_fallback() -> None:
    app = Hinoki(settings=AppSettings(allowed_hosts=("example.com",)))
    server = classifier(app)
    submission = server.submit("GET", "/anything", b"", [(b"host", b"example.com")])
    assert submission.early is None
    assert [request.action for request in server.drain(1)] == [None]
    denied = server.submit("GET", "/anything", b"", [(b"host", b"wrong.example")])
    assert denied.early is not None
    assert normalized(*denied.early)[0] == 400


def test_a_body_without_one_sentinel_is_refused() -> None:
    bad = [(kind, b"{}") for kind, _ in early_bodies()]
    with pytest.raises(ValueError, match="sentinels"):
        core.ServerCore([], bad, ([], []), b"x-request-id", "0123456789abcdef")


def test_bodies_are_rendered_with_the_sentinel() -> None:
    for kind, body in early_bodies():
        assert body.count(SENTINEL.encode()) == 1, kind


def cors_app() -> Hinoki:
    app = Hinoki(
        settings=AppSettings(cors=CorsSettings(origins=("https://a.example",)))
    )
    app.include(ping, native)
    return app


@pytest.mark.parametrize(
    "settings",
    [
        AppSettings(allowed_hosts=("example.com",)),
        AppSettings(cors=CorsSettings(origins=("https://a.example",))),
        AppSettings(
            allowed_hosts=("example.com",),
            security=SecuritySettings(redirect_https=True),
        ),
    ],
)
async def test_native_policies_keep_simple_routes_eligible_for_native_handlers(
    pool: Pool, settings: AppSettings
) -> None:
    app = Hinoki(settings=settings)
    app.include(native)
    await pool.start(app)
    server = classifier(app)
    headers = [(b"host", b"example.com"), (b"origin", b"https://a.example")]
    submission = server.submit("GET", "/native", b"", headers, https=True)
    answer = await native_final(app, server, submission)
    assert answer[0] == 200
    assert answer[2] == b'{"native":"yes"}'
    response = await app.asgi().pipeline.handle(
        scope_of("GET", "/native", "", headers, "https"), EmptyBody(), SENTINEL
    )
    assert final_view(*answer) == final_view(
        response.status, response.headers, response.body
    )


def scope_of(
    method: str, path: str, query: str, headers: list[tuple[bytes, bytes]], scheme: str
) -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": headers,
        "scheme": scheme,
    }


async def native_final(
    app: Hinoki, server: core.ServerCore, submission: core.Submission
) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    """Serve the queued native-handler request as the Rust worker does."""
    [request] = server.drain(1)
    assert request.native_handler is True
    assert request.action is not None
    pipeline = app.asgi().pipeline
    response = await pipeline.handle_native(
        pipeline.bindings[request.action],
        request.request_id,
        request.native_args,
        request.native_error,
    )
    assert request.respond_final(response.status, response.headers, response.body)
    reply = submission.reply()
    assert reply is not None
    return reply


def final_view(
    status: int, headers: list[tuple[bytes, bytes]], body: bytes
) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    """The final answer in order, with its request id masked everywhere."""
    rid = dict(headers)[b"x-request-id"]
    masked = [(k, b"<id>" if k == b"x-request-id" else v) for k, v in headers]
    return status, masked, body.replace(rid, b"<id>")


@pytest.mark.parametrize("origin", [b"https://a.example", b"https://b.example", b""])
async def test_cors_headers_on_native_answers_match_the_pipeline(
    pool: Pool, origin: bytes
) -> None:
    app = cors_app()
    client = await pool.start(app)
    headers = [(b"origin", origin)] if origin else []
    query = "q=" + "x" * 9000
    reply = await client.request("GET", "/ping", query=query, raw_headers=headers)
    submission = classifier(app).submit("GET", "/ping", query.encode(), headers)
    assert submission.early is not None
    assert normalized(*submission.early) == expected(reply)


async def test_host_cors_and_redirect_policies_match_pipeline_precedence(
    pool: Pool,
) -> None:
    app = Hinoki(
        settings=AppSettings(
            allowed_hosts=("example.com",),
            cors=CorsSettings(origins=("https://a.example",)),
            security=SecuritySettings(redirect_https=True),
        )
    )
    app.include(native)
    await pool.start(app)
    allowed = (b"host", b"example.com")
    listed = (b"origin", b"https://a.example")
    preflight = (b"access-control-request-method", b"GET")
    cases = [
        ("GET", "", [(b"host", b"wrong.example"), listed], 400, "http"),
        ("GET", "", [(b"host", b"\xa0example.com\xa0")], 200, "http"),
        (
            "OPTIONS",
            "",
            [allowed, (b"origin", b"https://evil.example"), preflight],
            403,
            "http",
        ),
        ("OPTIONS", "", [allowed, listed, preflight], 204, "http"),
        ("GET", "q=hello world", [allowed, listed], 308, "http"),
        ("GET", "", [allowed, listed], 200, "https"),
        ("GET", "", [allowed, (b"origin", b"https://evil.example")], 200, "https"),
        ("HEAD", "", [allowed, listed], 200, "https"),
        ("GET", "", [allowed, listed, listed], 200, "https"),
    ]
    for method, query, headers, status, scheme in cases:
        response = await app.asgi().pipeline.handle(
            scope_of(method, "/native", query, headers, scheme), EmptyBody(), SENTINEL
        )
        server = classifier(app)
        submission = server.submit(
            method, "/native", query.encode(), headers, https=scheme == "https"
        )
        answer = submission.early or await native_final(app, server, submission)
        body = b"" if method == "HEAD" else response.body
        assert response.status == status
        assert final_view(*answer) == final_view(
            response.status, response.headers, body
        )


def test_an_asterisk_target_is_not_redirected() -> None:
    app = Hinoki(
        settings=AppSettings(
            allowed_hosts=("example.com",),
            security=SecuritySettings(redirect_https=True),
        )
    )
    app.include(native)
    server = classifier(app)
    submission = server.submit("OPTIONS", "*", b"", [(b"host", b"example.com")])
    assert submission.early is None  # the Python pipeline answers it
    [request] = server.drain(1)
    assert request.action is None


def test_https_selects_the_https_policy_headers() -> None:
    app = Hinoki(
        settings=AppSettings(
            security=SecuritySettings(hsts_seconds=31_536_000),
            server=ServerSettings(trusted_proxies=("10.0.0.0/8",)),
        )
    )
    app.include(ping)
    server = classifier(app)
    names = [
        {k for k, _ in server.submit("GET", "/nope", b"", [], https=secure).early[1]}  # type: ignore[index]
        for secure in (False, True)
    ]
    assert b"strict-transport-security" not in names[0]
    assert b"strict-transport-security" in names[1]


def inbound_core() -> core.ServerCore:
    return core.ServerCore(
        [],
        early_bodies(),
        ([], []),
        b"x-request-id",
        "0123456789abcdef",
        trust_request_id=True,
    )


def test_an_inbound_request_id_is_used_only_from_a_trusted_peer() -> None:
    server = inbound_core()
    server.set_ready(True)
    sent = [(b"x-request-id", b"abc-123")]
    untrusted = server.submit("GET", "/x", b"", sent).early
    trusted = server.submit("GET", "/x", b"", sent, trusted=True).early
    assert untrusted is not None
    assert trusted is not None
    assert dict(untrusted[1])[b"x-request-id"] != b"abc-123"
    assert dict(trusted[1])[b"x-request-id"] == b"abc-123"


def test_a_trusted_id_with_quotes_keeps_the_body_valid_json() -> None:
    server = inbound_core()
    server.set_ready(True)
    hostile = b'a"}\\'
    early = server.submit(
        "GET", "/x", b"", [(b"x-request-id", hostile)], trusted=True
    ).early
    assert early is not None
    assert json.loads(early[2])["request_id"] == hostile.decode()


def test_the_slot_is_freed_when_the_request_is_answered() -> None:
    server = classifier(make_app())
    server.submit("GET", "/ping", b"", [])
    [request] = server.drain(1)
    assert server.in_flight == 1
    request.respond(200, [], b"")
    assert server.in_flight == 0  # while Python still holds the object


def test_reply_is_stable_and_a_dropped_request_is_an_error() -> None:
    server = classifier(make_app())
    answered = server.submit("GET", "/ping", b"", [])
    [request] = server.drain(1)
    request.respond(200, [], b"ok")
    assert answered.reply() == answered.reply() == (200, [], b"ok")
    lost = server.submit("GET", "/ping", b"", [])
    server.drain(1).clear()
    with pytest.raises(RuntimeError, match="dropped"):
        lost.reply()


def test_python_paths_are_compared_decoded_and_odd_targets_go_to_python() -> None:
    app = Hinoki(openapi_url="/openapi.json", docs_url="/docs")
    app.include(ping)
    server = classifier(app)
    server.submit("GET", "/openapi%2Ejson", b"", [])
    server.submit("GET", "/café", b"", [])
    assert [r.action for r in server.drain(10)] == [None, None]
