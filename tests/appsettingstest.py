"""The ``debug``, ``allowed_hosts`` and ``cors`` settings and what they change."""

import io
import json
import logging
from typing import Any

import pytest

from hinoki import (
    API,
    Access,
    BearerAuth,
    Caller,
    Hinoki,
    Request,
    Response,
    errors,
    http,
)
from hinoki.__main__ import main
from hinoki.admin import Admin
from hinoki.runtime.config import RuntimeConfig
from hinoki.runtime.origins import HostPolicy, host_name
from hinoki.scenario import build_scope, drive
from hinoki.settings import AppSettings, ServerSettings, SettingsError
from hinoki.testing import ASGIClient, SyncClient
from hinoki.wsgi import WSGIAdapter
from tests.harness import Client

APP = "https://app.example.com"


class Auth(BearerAuth[str]):
    async def resolve_token(self, token: str) -> Caller[str] | None:
        return None


class OutOfStock(errors.APIError):
    status = 409
    code = "out_of_stock"
    message = "nothing left"


@http.get("/ping")
async def ping() -> dict[str, str]:
    return {"pong": "ok"}


@http.get("/private", access=Access(auth=Auth()))
async def private() -> dict[str, str]:
    return {"secret": "x"}


@http.get("/boom")
async def boom() -> dict[str, str]:
    raise RuntimeError("the cache is cold")


@http.get("/declared", errors=[OutOfStock])
async def declared() -> dict[str, str]:
    raise OutOfStock()


def load(**env: str) -> AppSettings:
    return AppSettings.load(prefix="APP_", env=env)


def app_of(**env: str) -> Hinoki:
    return Hinoki(settings=load(**env), include=[ping, private, boom, declared])


def trusted_proxy_settings() -> AppSettings:
    return AppSettings(
        server=ServerSettings(
            trusted_proxies=("10.0.0.0/8",),
            request_id_header="x-correlation-id",
            trust_request_id=True,
        )
    )


def problem(**env: str) -> str:
    with pytest.raises(SettingsError) as caught:
        load(**env)
    return str(caught.value)


async def raw_request(
    app: Hinoki,
    method: str,
    path: str,
    headers: list[tuple[bytes, bytes]],
    query: bytes = b"",
    client: tuple[str, int] = ("127.0.0.1", 12345),
    scheme: str = "http",
) -> dict[str, Any]:
    """The response start for a scope with repeated headers, which the test
    client cannot send."""
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    adapter = app.asgi()
    adapter.lifespan._started = True  # serve without the lifespan protocol
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query,
        "client": client,
        "scheme": scheme,
        "server": ("example.test", 80),
    }
    await adapter({**scope, "headers": headers}, None, send)
    sent[0]["response_body"] = sent[1]["body"]
    return sent[0]


@http.get("/proxy-info")
async def proxy_info(request: Request) -> dict[str, Any]:
    return {
        "scheme": request.scheme,
        "client": request.client,
        "host": request.host,
        "request_id": request.request_id,
    }


def test_trusted_proxy_settings_validate_networks_and_request_header() -> None:
    settings = load(
        APP_SERVER__TRUSTED_PROXIES="10.0.0.0/8,2001:db8::1",
        APP_SERVER__REQUEST_ID_HEADER="x-correlation-id",
        APP_SERVER__TRUST_REQUEST_ID="true",
    )
    assert settings.server.trusted_proxies == ("10.0.0.0/8", "2001:db8::1")
    assert settings.server.request_id_header == "x-correlation-id"
    assert settings.server.trust_request_id is True
    assert "trusted_proxies entries" in problem(
        APP_SERVER__TRUSTED_PROXIES="10.0.0.1/not-a-mask"
    )
    assert "request_id_header must be an HTTP token" in problem(
        APP_SERVER__REQUEST_ID_HEADER="x bad"
    )
    assert "conflicts with a framework header" in problem(
        APP_SERVER__REQUEST_ID_HEADER="content-length"
    )
    assert "conflicts with a framework header" in problem(
        APP_SERVER__REQUEST_ID_HEADER="Access-Control-Allow-Origin"
    )
    assert "conflicts with a framework header" in problem(
        APP_SERVER__REQUEST_ID_HEADER="content-security-policy"
    )
    assert "conflicts with a framework header" in problem(
        APP_SERVER__REQUEST_ID_HEADER="www-authenticate"
    )
    assert "conflicts with a framework header" in problem(
        APP_SERVER__REQUEST_ID_HEADER="allow"
    )
    assert "conflicts with a framework header" in problem(
        APP_SERVER__REQUEST_ID_HEADER="retry-after"
    )


async def test_proxy_metadata_and_request_id_require_a_trusted_peer() -> None:
    settings = trusted_proxy_settings()
    app = Hinoki(settings=settings, include=[proxy_info])
    sent = await raw_request(
        app,
        "GET",
        "/proxy-info",
        [
            (b"host", b"raw.example.test"),
            (b"x-forwarded-host", b"forged.example.test"),
            (b"x-forwarded-proto", b"https"),
            (b"x-forwarded-for", b"198.51.100.9, 10.1.2.3"),
            (b"x-correlation-id", b"forged-id"),
        ],
        client=("192.0.2.1", 1234),
    )
    payload = json.loads(sent["response_body"])
    assert payload["scheme"] == "http"
    assert payload["client"] == ["192.0.2.1", 1234]
    assert payload["host"] == "raw.example.test"
    assert payload["request_id"] != "forged-id"
    assert (b"x-correlation-id", payload["request_id"].encode()) in sent["headers"]


async def test_trusted_proxy_resolves_client_scheme_and_valid_request_id() -> None:
    settings = trusted_proxy_settings()
    app = Hinoki(settings=settings, include=[proxy_info])
    sent = await raw_request(
        app,
        "GET",
        "/proxy-info",
        [
            (b"host", b"raw.example.test"),
            (b"x-forwarded-host", b"forged.example.test"),
            (b"x-forwarded-proto", b"https"),
            (b"x-forwarded-for", b"198.51.100.9, 10.1.2.3"),
            (b"x-correlation-id", b"gateway-42"),
        ],
        client=("10.2.3.4", 443),
    )
    payload = json.loads(sent["response_body"])
    assert payload == {
        "scheme": "https",
        "client": ["198.51.100.9", 0],
        "host": "raw.example.test",
        "request_id": "gateway-42",
    }
    assert (b"x-correlation-id", b"gateway-42") in sent["headers"]


@pytest.mark.parametrize(
    ("forwarded_for", "request_id"),
    [
        (b"198.51.100.9", b"two,values"),
        (b"bad-address", b"bad\x00value"),
        (b"198.51.100.9,10.1.2.3,10.2.3.4", b""),
    ],
)
async def test_ambiguous_proxy_metadata_falls_back_to_socket_and_generated_id(
    forwarded_for: bytes, request_id: bytes
) -> None:
    settings = trusted_proxy_settings()
    app = Hinoki(settings=settings, include=[proxy_info])
    sent = await raw_request(
        app,
        "GET",
        "/proxy-info",
        [
            (b"host", b"raw.example.test"),
            (b"x-forwarded-proto", b"https, http"),
            (b"x-forwarded-for", forwarded_for),
            (b"x-correlation-id", request_id),
        ],
        client=("10.2.3.4", 443),
    )
    payload = json.loads(sent["response_body"])
    assert payload["scheme"] == "http"
    expected_client = (
        ["10.2.3.4", 443] if forwarded_for == b"bad-address" else ["198.51.100.9", 0]
    )
    assert payload["client"] == expected_client
    assert payload["request_id"] != request_id.decode("latin-1")


async def test_repeated_forwarding_headers_are_ignored() -> None:
    settings = trusted_proxy_settings()
    app = Hinoki(settings=settings, include=[proxy_info])
    sent = await raw_request(
        app,
        "GET",
        "/proxy-info",
        [
            (b"host", b"raw.example.test"),
            (b"x-forwarded-proto", b"https"),
            (b"x-forwarded-proto", b"http"),
            (b"x-forwarded-for", b"198.51.100.9"),
            (b"x-forwarded-for", b"203.0.113.8"),
            (b"x-correlation-id", b"gateway-42"),
        ],
        client=("10.2.3.4", 443),
    )
    payload = json.loads(sent["response_body"])
    assert payload["scheme"] == "http"
    assert payload["client"] == ["10.2.3.4", 443]
    assert payload["request_id"] == "gateway-42"


async def test_forwarded_chain_without_an_untrusted_client_uses_socket_peer() -> None:
    settings = AppSettings(server=ServerSettings(trusted_proxies=("10.0.0.0/8",)))
    app = Hinoki(settings=settings, include=[proxy_info])
    sent = await raw_request(
        app,
        "GET",
        "/proxy-info",
        [
            (b"host", b"raw.example.test"),
            (b"x-forwarded-for", b"10.1.1.1"),
        ],
        client=("10.2.3.4", 443),
    )
    payload = json.loads(sent["response_body"])
    assert payload["client"] == ["10.2.3.4", 443]


def test_wsgi_uses_the_same_trusted_proxy_policy() -> None:
    settings = trusted_proxy_settings()
    adapter = WSGIAdapter(Hinoki(settings=settings, include=[proxy_info]))
    answer: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        answer["status"] = status
        answer["headers"] = dict(headers)

    try:
        body = b"".join(
            adapter(
                {
                    "REQUEST_METHOD": "GET",
                    "PATH_INFO": "/proxy-info",
                    "QUERY_STRING": "",
                    "SERVER_PROTOCOL": "HTTP/1.1",
                    "SERVER_NAME": "example.test",
                    "SERVER_PORT": "80",
                    "REMOTE_ADDR": "10.1.2.3",
                    "wsgi.url_scheme": "http",
                    "wsgi.input": io.BytesIO(),
                    "CONTENT_LENGTH": "0",
                    "HTTP_HOST": "raw.example.test",
                    "HTTP_X_FORWARDED_PROTO": "https",
                    "HTTP_X_FORWARDED_FOR": "198.51.100.9",
                    "HTTP_X_CORRELATION_ID": "gateway-42",
                },
                start_response,
            )
        )
    finally:
        adapter.close()
    assert answer["status"].startswith("200 ")
    assert answer["headers"]["x-correlation-id"] == "gateway-42"
    assert json.loads(body) == {
        "scheme": "https",
        "client": ["198.51.100.9", 0],
        "host": "raw.example.test",
        "request_id": "gateway-42",
    }


def test_the_defaults_change_nothing() -> None:
    settings = load()
    assert settings.debug is False
    assert settings.allowed_hosts == ("*",)
    assert settings.cors.origins == ()


def test_request_byte_limits_load_and_validate() -> None:
    settings = load(
        APP_EXECUTION__MAX_QUERY_BYTES="4096",
        APP_EXECUTION__MAX_HEADER_BYTES="16384",
    )
    assert settings.execution.max_query_bytes == 4096
    assert settings.execution.max_header_bytes == 16384
    assert "max_query_bytes must be 1 to" in problem(APP_EXECUTION__MAX_QUERY_BYTES="0")
    assert "max_header_bytes must be 1 to" in problem(
        APP_EXECUTION__MAX_HEADER_BYTES="1048577"
    )
    with pytest.raises(ValueError, match="max_query_bytes"):
        RuntimeConfig(max_query_bytes=0)
    with pytest.raises(ValueError, match="max_header_bytes"):
        RuntimeConfig(max_header_bytes=1 << 21)


@pytest.mark.asyncio
async def test_oversized_query_and_headers_are_rejected_before_routing() -> None:
    query_app = Hinoki(runtime=RuntimeConfig(max_query_bytes=3), include=[ping])
    accepted_query = await raw_request(query_app, "GET", "/ping", [], query=b"a=1")
    query = await raw_request(query_app, "GET", "/ping", [], query=b"a=123")
    assert accepted_query["status"] == 200 and query["status"] == 414

    header_app = Hinoki(runtime=RuntimeConfig(max_header_bytes=12), include=[ping])
    accepted_headers = await raw_request(
        header_app, "GET", "/ping", [(b"x", b"1234567")]
    )
    headers = await raw_request(header_app, "GET", "/ping", [(b"x", b"12345678")])
    assert accepted_headers["status"] == 200 and headers["status"] == 431


def test_values_load_from_the_environment() -> None:
    settings = load(
        APP_DEBUG="true",
        APP_ALLOWED_HOSTS="api.example.com, .example.org",
        APP_CORS__ORIGINS=f"{APP},http://localhost:5173",
        APP_CORS__CREDENTIALS="yes",
        APP_CORS__MAX_AGE="60",
    )
    assert settings.debug is True
    assert settings.allowed_hosts == ("api.example.com", ".example.org")
    assert settings.cors.origins == (APP, "http://localhost:5173")
    assert settings.cors.credentials is True and settings.cors.max_age == 60


@pytest.mark.parametrize(
    "hosts",
    [
        "https://api.example.com",
        "api.example.com:8000",
        "api.example.com/path",
        "*.example.com",
        "api example.com",
        "-bad.example.com",
        "1:2",
        "..:",
        "::1,[::1]",
        "bücher.example",
        "fe80::1%ü",
    ],
)
def test_a_host_entry_is_a_name_not_a_url(hosts: str) -> None:
    message = problem(APP_ALLOWED_HOSTS=hosts)
    assert "APP_: allowed_hosts entries are host names" in message
    assert hosts not in message  # values are never echoed


def test_any_host_is_not_mixed_with_names() -> None:
    assert "'*' must be the only entry" in problem(APP_ALLOWED_HOSTS="*,a.test")


def test_ip_addresses_are_host_entries() -> None:
    settings = load(APP_ALLOWED_HOSTS="127.0.0.1,::1")
    assert settings.allowed_hosts == ("127.0.0.1", "::1")


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        ({"APP_CORS__ORIGINS": "app.example.com"}, "scheme://host[:port]"),
        ({"APP_CORS__ORIGINS": f"{APP}/"}, "scheme://host[:port]"),
        ({"APP_CORS__ORIGINS": f"*,{APP}"}, "must be the only entry"),
        (
            {"APP_CORS__ORIGINS": "*", "APP_CORS__CREDENTIALS": "true"},
            "cannot be combined with credentials",
        ),
        ({"APP_CORS__ORIGINS": APP, "APP_CORS__METHODS": "GET POST"}, "token"),
        ({"APP_CORS__ORIGINS": APP, "APP_CORS__HEADERS": "x:y"}, "token"),
        ({"APP_CORS__MAX_AGE": "-1"}, "max_age"),
        ({"APP_CORS__MAX_AGE": "86401"}, "max_age"),
    ],
)
def test_cors_mistakes_fail_at_load(env: dict[str, str], reason: str) -> None:
    message = problem(**env)
    assert "APP_CORS" in message and reason in message


@pytest.mark.parametrize(
    ("header", "name"),
    [
        ("API.Example.com", "api.example.com"),
        ("api.example.com:8443", "api.example.com"),
        ("api.example.com.", "api.example.com"),
        ("[::1]:8000", "::1"),
        ("[::1]", "::1"),
        ("[0:0:0:0:0:0:0:1]:80", "::1"),
        ("", ""),
        ("api.example.com:", ""),
        ("api.example.com:\u00b2", ""),
        ("api.example.com:123456", ""),
        ("evil.test, x.example.org", ""),
        ("evil.test/.example.org", ""),
        ("evil.test@x.example.org", ""),
        ("evil.test x.example.org", ""),
        ("[evil.test/.example.org]", ""),
        (".example.org", ""),
        ("a..example.org", ""),
        ("[1:2]", ""),
        ("api.example.com:port", ""),
        ("api.example.com:1:2", ""),
        ("[::1", ""),
        ("[::1]8000", ""),
        ("[::1]:", ""),
    ],
)
def test_the_host_header_is_reduced_to_its_name(header: str, name: str) -> None:
    assert host_name(header) == name


def test_host_patterns_match_exact_names_and_subdomains() -> None:
    policy = HostPolicy.of(
        load(APP_ALLOWED_HOSTS="api.example.com,.example.org,0:0:0:0:0:0:0:1")
    )
    assert policy is not None
    allowed = ["api.example.com", "example.org", "a.b.example.org:80", "[::1]:8000"]
    refused = [
        "example.com",
        "evil-example.org",
        "api.example.com.evil.test",
        "evil.test/.example.org",
        "evil.test, x.example.org",
        "",
    ]
    assert all(policy.allows(host) for host in allowed)
    assert not any(policy.allows(host) for host in refused)
    assert HostPolicy.of(load()) is None  # any host: nothing to check
    assert HostPolicy.of(None) is None


async def test_a_disallowed_host_is_refused_before_authentication() -> None:
    app = app_of(APP_ALLOWED_HOSTS="api.example.com", APP_DOCS__OPENAPI_URL="/openapi")
    async with ASGIClient(app) as client:
        good = {"host": "api.example.com:8000"}
        assert (await client.get("/ping", headers=good)).status == 200
        for path in ("/ping", "/private", "/openapi", "/nope"):
            refused = await client.get(path, headers={"host": "evil.test"})
            assert refused.status == 400
            assert refused.body["error"] == "disallowed_host"
            assert "evil.test" not in json.dumps(refused.body)
        assert (await client.get("/ping")).status == 400  # no Host header at all


async def test_two_host_headers_never_pass_the_check() -> None:
    app = app_of(APP_ALLOWED_HOSTS=".example.org")
    hosts = [(b"host", b"evil.test"), (b"host", b"x.example.org")]
    assert (await raw_request(app, "GET", "/ping", hosts))["status"] == 400


async def test_cors_is_off_until_an_origin_is_configured() -> None:
    async with ASGIClient(app_of()) as client:
        reply = await client.get("/ping", headers={"origin": APP})
        assert not any(name.startswith("access-control") for name in reply.headers)
        preflight = await client.request(
            "OPTIONS",
            "/ping",
            headers={"origin": APP, "access-control-request-method": "GET"},
        )
        assert preflight.status == 405


async def test_an_allowed_origin_can_read_responses_and_errors() -> None:
    app = app_of(
        APP_CORS__ORIGINS=APP,
        APP_CORS__CREDENTIALS="true",
        APP_CORS__EXPOSE_HEADERS="X-Request-Id",
    )
    async with ASGIClient(app) as client:
        for path, status in (("/ping", 200), ("/private", 401), ("/nope", 404)):
            reply = await client.get(path, headers={"origin": APP})
            assert reply.status == status
            assert reply.headers["access-control-allow-origin"] == APP
            assert reply.headers["access-control-allow-credentials"] == "true"
            assert reply.headers["access-control-expose-headers"] == "x-request-id"
            assert reply.headers["vary"] == "origin"
        for origin in ("https://evil.test", APP.upper(), "null"):
            other = await client.get("/ping", headers={"origin": origin})
            assert other.status == 200  # served; the browser blocks the read
            assert "access-control-allow-origin" not in other.headers
            assert other.headers["vary"] == "origin"  # caches keep them apart
        plain = await client.get("/ping")
        assert "access-control-allow-origin" not in plain.headers
        assert plain.headers["vary"] == "origin"


@http.get("/own")
async def own_cors() -> Response:
    return Response(
        200, b"x", headers=[("Access-Control-Allow-Origin", "https://o.test")]
    )


async def test_an_answer_with_its_own_cors_headers_keeps_them() -> None:
    app = Hinoki(settings=load(APP_CORS__ORIGINS=APP), include=[own_cors])
    start = await raw_request(app, "GET", "/own", [(b"origin", APP.encode())])
    allow = [v for k, v in start["headers"] if k == b"access-control-allow-origin"]
    assert allow == [b"https://o.test"]


async def test_two_origin_headers_are_not_an_allowed_origin() -> None:
    app = app_of(APP_CORS__ORIGINS=APP, APP_CORS__CREDENTIALS="true")
    headers = [
        (b"origin", APP.encode()),
        (b"origin", b"https://evil.test"),
        (b"access-control-request-method", b"GET"),
    ]
    start = await raw_request(app, "OPTIONS", "/ping", headers)
    names = {name for name, _ in start["headers"]}
    assert start["status"] == 403
    assert b"access-control-allow-origin" not in names
    assert b"access-control-allow-credentials" not in names


async def test_any_origin_is_answered_with_a_wildcard() -> None:
    async with ASGIClient(app_of(APP_CORS__ORIGINS="*")) as client:
        reply = await client.get("/ping", headers={"origin": "https://anywhere.test"})
        assert reply.headers["access-control-allow-origin"] == "*"
        assert "vary" not in reply.headers
        assert "access-control-allow-credentials" not in reply.headers


async def test_a_preflight_is_answered_without_reaching_the_route() -> None:
    app = app_of(APP_CORS__ORIGINS=APP, APP_CORS__HEADERS="Authorization,X-Trace")
    ask = {"origin": APP, "access-control-request-method": "DELETE"}
    async with ASGIClient(app) as client:
        reply = await client.request("OPTIONS", "/private", headers=ask)
        assert (reply.status, reply.raw) == (204, b"")
        assert reply.headers["access-control-allow-origin"] == APP
        assert reply.headers["access-control-allow-methods"] == (
            "GET, POST, PUT, PATCH, DELETE, QUERY"
        )
        assert reply.headers["access-control-allow-headers"] == "authorization, x-trace"
        assert reply.headers["access-control-max-age"] == "600"
        denied = await client.request(
            "OPTIONS", "/private", headers={**ask, "origin": "https://evil.test"}
        )
        assert (denied.status, denied.body["error"]) == (403, "cors_origin_denied")
        assert "access-control-allow-origin" not in denied.headers
        assert denied.headers["vary"] == "origin"
        # Not a preflight: no Access-Control-Request-Method, or no Origin.
        for headers in ({"origin": APP}, {"access-control-request-method": "GET"}):
            plain = await client.request("OPTIONS", "/ping", headers=headers)
            assert plain.status == 405


async def test_debug_describes_an_unhandled_exception_in_the_answer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="hinoki.runtime"):
        async with ASGIClient(app_of(APP_DEBUG="true")) as client:
            reply = await client.get("/boom")
            known = await client.get("/declared")
    assert reply.status == 500 and reply.body["error"] == "internal_error"
    debug = reply.body["debug"]
    assert debug["exception"] == "builtins.RuntimeError"
    assert debug["message"] == "the cache is cold"
    assert any("raise RuntimeError" in line for line in debug["traceback"])
    assert known.status == 409 and "debug" not in known.body  # declared: unchanged
    assert "debug is on" in caplog.text


async def test_without_debug_the_answer_names_only_the_request() -> None:
    for app in (app_of(), Hinoki(include=[boom])):
        async with ASGIClient(app) as client:
            reply = await client.get("/boom")
        assert reply.status == 500
        assert set(reply.body) == {"error", "request_id"}


def wsgi_get(adapter: WSGIAdapter, **headers: str) -> tuple[str, dict[str, str]]:
    seen: dict[str, Any] = {}

    def start_response(status: str, sent: list[tuple[str, str]]) -> None:
        seen["status"], seen["headers"] = status, dict(sent)

    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/ping",
        "SERVER_NAME": "t",
        "SERVER_PORT": "80",
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(b""),
        **headers,
    }
    b"".join(adapter(environ, start_response))
    return seen["status"], seen["headers"]


def test_wsgi_applies_query_and_header_byte_limits() -> None:
    adapter = WSGIAdapter(
        Hinoki(
            runtime=RuntimeConfig(max_query_bytes=3, max_header_bytes=12),
            include=[ping],
        )
    )
    try:
        assert wsgi_get(adapter, QUERY_STRING="a=123")[0].startswith("414 ")
        assert wsgi_get(adapter, HTTP_X="12345678")[0].startswith("431 ")
    finally:
        adapter.close()


def test_the_wsgi_adapter_applies_the_same_policies() -> None:
    adapter = WSGIAdapter(
        app_of(APP_ALLOWED_HOSTS="api.example.com", APP_CORS__ORIGINS=APP)
    )
    try:
        status, headers = wsgi_get(
            adapter, HTTP_HOST="api.example.com", HTTP_ORIGIN=APP
        )
        assert status == "200 OK"
        assert headers["access-control-allow-origin"] == APP
        assert wsgi_get(adapter, HTTP_HOST="evil.test")[0] == "400 Bad Request"
    finally:
        adapter.close()


def test_the_settings_command_lists_the_new_keys(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_CORS__ORIGINS", APP)
    assert main(["settings", "hinoki.settings:AppSettings", "--prefix", "APP_"]) == 0
    out = capsys.readouterr().out
    assert "APP_DEBUG" in out and "APP_ALLOWED_HOSTS" in out
    assert "APP_CORS__ORIGINS" in out and APP in out


def host_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "hinoki.runtime" and "allowed_hosts" in record.getMessage()
    ]


async def served_startup(app: Hinoki) -> None:
    """Start and stop the app the way a server does: a plain lifespan scope."""
    client = Client(app.asgi())
    assert await client.lifespan_event("startup") == "lifespan.startup.complete"
    assert await client.lifespan_event("shutdown") == "lifespan.shutdown.complete"


async def test_a_served_app_with_any_host_and_debug_off_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="hinoki.runtime"):
        await served_startup(app_of())
    [warning] = host_warnings(caplog)
    assert "allowed_hosts is '*'" in warning
    assert "Host header" in warning and "password-reset" in warning
    assert 'allowed_hosts=("app.example.com",)' in warning
    assert '".example.com"' in warning


@pytest.mark.parametrize(
    "env",
    [
        {"APP_DEBUG": "true"},
        {"APP_ALLOWED_HOSTS": "app.example.com"},
        {"APP_ALLOWED_HOSTS": "app.example.com, .example.org"},
        {"APP_ALLOWED_HOSTS": ".example.com"},
    ],
)
async def test_debug_or_named_hosts_do_not_warn(
    env: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="hinoki.runtime"):
        await served_startup(app_of(**env))
    assert host_warnings(caplog) == []


@http.websocket("/live")
async def live(socket: http.WebSocket) -> None:
    await socket.close()


def admin_app(**env: str) -> Hinoki:
    return Hinoki(settings=load(APP_ADMIN__SECRET="s" * 32, **env), include=[Admin()])


def socket_app(**env: str) -> Hinoki:
    return Hinoki(settings=load(**env), include=[live])


@pytest.mark.parametrize("make", [admin_app, socket_app])
async def test_admin_or_websockets_with_any_host_refuse_to_start(
    make: Any, caplog: pytest.LogCaptureFixture
) -> None:
    client = Client(make().asgi())
    with caplog.at_level(logging.ERROR, logger="hinoki.runtime"):
        assert await client.lifespan_event("startup") == "lifespan.startup.failed"
    assert any("allowed_hosts" in record.getMessage() for record in caplog.records)


async def test_an_app_without_settings_is_checked_like_any_host_any(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="hinoki.runtime"):
        await served_startup(Hinoki())
    assert len(host_warnings(caplog)) == 1
    caplog.clear()
    for app in (Hinoki(include=[live]), Hinoki(include=[Admin(secret="s" * 32)])):
        client = Client(app.asgi())
        with caplog.at_level(logging.ERROR, logger="hinoki.runtime"):
            assert await client.lifespan_event("startup") == "lifespan.startup.failed"
    assert len(host_warnings(caplog)) == 2


async def test_an_api_with_a_non_admin_admin_attribute_starts_with_any_host() -> None:
    class Users(API):
        admin = False

    app = Hinoki(settings=load(), include=[Users])
    assert app.has_admin is False
    await served_startup(app)


@pytest.mark.parametrize(
    "env", [{"APP_DEBUG": "true"}, {"APP_ALLOWED_HOSTS": "app.example.com"}]
)
async def test_websockets_start_with_debug_or_named_hosts(env: dict[str, str]) -> None:
    await served_startup(socket_app(**env))


async def test_the_in_process_client_runs_websocket_apps_with_any_host() -> None:
    async with ASGIClient(socket_app()) as client:
        assert (await client.get("/missing")).status == 404


def test_the_wsgi_adapter_warns_like_a_server(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = WSGIAdapter(app_of())
    with caplog.at_level(logging.WARNING, logger="hinoki.runtime"):
        try:
            assert wsgi_get(adapter)[0] == "200 OK"
        finally:
            adapter.close()
    assert len(host_warnings(caplog)) == 1


async def test_the_in_process_drivers_do_not_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="hinoki.runtime"):
        async with ASGIClient(app_of()) as client:
            assert (await client.get("/ping")).status == 200
        with SyncClient(app_of()) as sync:
            assert sync.get("/ping").status == 200
        scope = build_scope("GET", "/ping", None, None, "read")
        assert (await drive(app_of(), scope, b""))["status"] == 200
    assert host_warnings(caplog) == []
