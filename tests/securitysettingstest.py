"""Typed response security settings, HTTPS redirects and proxy-aware HSTS."""

from __future__ import annotations

import io
from typing import Any

import pytest

from hinoki import Access, BearerAuth, Caller, Hinoki, Reply, http
from hinoki.admin import Admin
from hinoki.settings import (
    AppSettings,
    CorsSettings,
    DocsSettings,
    FrameOptions,
    ReferrerPolicy,
    SecuritySettings,
    ServerSettings,
    SettingsError,
)
from hinoki.testing import ASGIClient
from hinoki.wsgi import WSGIAdapter


async def raw_request(
    app: Hinoki,
    method: str,
    path: str,
    headers: list[tuple[bytes, bytes]],
    *,
    query: bytes = b"",
    client: tuple[str, int] = ("127.0.0.1", 4000),
    scheme: str = "http",
) -> dict[str, Any]:
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    adapter = app.asgi()
    adapter.lifespan._started = True
    await adapter(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query,
            "headers": headers,
            "scheme": scheme,
            "client": client,
            "server": ("api.example.test", 80),
        },
        None,
        send,
    )
    sent[0]["response_body"] = sent[1]["body"]
    return sent[0]


@http.get("/secure")
async def secure() -> dict[str, str]:
    return {"ok": "yes"}


class RejectAuth(BearerAuth[str]):
    async def resolve_token(self, token: str) -> Caller[str] | None:
        return None


@http.post("/submit")
async def submit() -> dict[str, str]:
    return {"ok": "yes"}


@http.get("/custom-security")
async def custom_security() -> Reply:
    return Reply(
        200,
        b"ok",
        headers=[
            ("x-frame-options", "DENY"),
            ("referrer-policy", "origin"),
            ("strict-transport-security", "max-age=999999"),
        ],
    )


def configured(**values: object) -> AppSettings:
    return AppSettings(
        allowed_hosts=("api.example.test",),
        security=SecuritySettings(**values),  # type: ignore[arg-type]
    )


def test_security_enums_load_as_closed_values_and_from_toml(tmp_path: Any) -> None:
    settings = AppSettings.load(
        prefix="APP_",
        env={
            "APP_SECURITY__FRAME_OPTIONS": "deny",
            "APP_SECURITY__REFERRER_POLICY": "same-origin",
        },
    )
    assert settings.security.frame_options is FrameOptions.DENY
    assert settings.security.referrer_policy is ReferrerPolicy.SAME_ORIGIN
    config = tmp_path / "settings.toml"
    config.write_text('[security]\nframe_options = "deny"\n', encoding="utf-8")
    from_toml = AppSettings.load(toml=config)
    assert from_toml.security.frame_options is FrameOptions.DENY
    with pytest.raises(SettingsError, match="must be one of"):
        AppSettings.load(
            prefix="APP_", env={"APP_SECURITY__FRAME_OPTIONS": "allow-from"}
        )


def test_hsts_and_redirect_configuration_is_validated() -> None:
    with pytest.raises(ValueError, match="frame_options must be a FrameOptions"):
        SecuritySettings(frame_options="deny")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="require hsts_seconds"):
        SecuritySettings(hsts_preload=True)
    with pytest.raises(ValueError, match="include_subdomains and at least"):
        SecuritySettings(hsts_seconds=31_536_000, hsts_preload=True)
    with pytest.raises(SettingsError, match="requires explicit allowed_hosts"):
        AppSettings.load(prefix="APP_", env={"APP_SECURITY__REDIRECT_HTTPS": "true"})


async def test_default_security_headers_cover_success_errors_and_docs() -> None:
    app = Hinoki(
        settings=AppSettings(
            docs=DocsSettings(openapi_url="/openapi.json", docs_url="/docs")
        ),
        include=[secure],
    )
    async with ASGIClient(app) as client:
        good = await client.get("/secure")
        missing = await client.get("/missing")
        docs = await client.get("/docs")
    assert good.headers["x-frame-options"] == "SAMEORIGIN"
    assert good.headers["referrer-policy"] == "same-origin"
    assert good.headers["x-content-type-options"] == "nosniff"
    assert missing.status == 404
    assert missing.headers["x-frame-options"] == "SAMEORIGIN"
    assert docs.status == 200 and docs.headers["x-content-type-options"] == "nosniff"


async def test_default_referrer_policy_preserves_admin_privacy() -> None:
    admin = Admin(access=Access(auth=RejectAuth()), secret="s" * 32)
    app = Hinoki(include=[admin.api()])
    css = await raw_request(
        app,
        "GET",
        "/admin/static/admin.css",
        [(b"host", b"testserver")],
    )
    headers = dict(css["headers"])
    assert css["status"] == 200
    assert headers[b"referrer-policy"] == b"same-origin"


async def test_security_settings_override_route_headers_without_duplicates() -> None:
    app = Hinoki(
        settings=configured(
            frame_options=FrameOptions.DENY,
            referrer_policy=ReferrerPolicy.NO_REFERRER,
        ),
        include=[custom_security],
    )
    async with ASGIClient(app) as client:
        reply = await client.get("/custom-security")
    assert reply.headers["x-frame-options"] == "DENY"
    assert reply.headers["referrer-policy"] == "no-referrer"
    assert "strict-transport-security" not in reply.headers
    raw = await raw_request(
        app,
        "GET",
        "/custom-security",
        [(b"host", b"api.example.test")],
    )
    assert sum(k == b"x-frame-options" for k, _ in raw["headers"]) == 1


async def test_http_redirect_is_308_and_preserves_raw_target_before_handler() -> None:
    app = Hinoki(
        settings=configured(redirect_https=True),
        include=[submit],
    )
    received = False

    async def receive() -> dict[str, Any]:
        nonlocal received
        received = True
        return {"type": "http.request", "body": b"payload", "more_body": False}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    adapter = app.asgi()
    adapter.lifespan._started = True
    await adapter(
        {
            "type": "http",
            "method": "POST",
            "path": "/submit",
            "raw_path": b"/submit%20now",
            "query_string": b"next=%2Fhome",
            "headers": [(b"host", b"api.example.test:80")],
            "scheme": "http",
            "client": ("127.0.0.1", 4000),
            "server": ("api.example.test", 80),
        },
        receive,
        send,
    )
    assert sent[0]["status"] == 308
    assert dict(sent[0]["headers"])[b"location"] == (
        b"https://api.example.test/submit%20now?next=%2Fhome"
    )
    assert not received


async def test_https_redirect_does_not_loop_and_preflight_stays_local_http() -> None:
    app = Hinoki(
        settings=AppSettings(
            allowed_hosts=("api.example.test",),
            cors=CorsSettings(origins=("https://client.example.test",)),
            security=SecuritySettings(redirect_https=True),
        ),
        include=[secure],
    )
    https = await raw_request(
        app, "GET", "/secure", [(b"host", b"api.example.test")], scheme="https"
    )
    preflight = await raw_request(
        app,
        "OPTIONS",
        "/secure",
        [
            (b"host", b"api.example.test"),
            (b"origin", b"https://client.example.test"),
            (b"access-control-request-method", b"GET"),
        ],
    )
    assert https["status"] == 200
    assert preflight["status"] == 204


async def test_hsts_requires_effective_https_from_a_trusted_proxy() -> None:
    app = Hinoki(
        settings=AppSettings(
            allowed_hosts=("api.example.test",),
            server=ServerSettings(trusted_proxies=("10.0.0.0/8",)),
            security=SecuritySettings(
                hsts_seconds=31_536_000,
                hsts_include_subdomains=True,
                hsts_preload=True,
            ),
        ),
        include=[secure],
    )
    headers = [(b"host", b"api.example.test"), (b"x-forwarded-proto", b"https")]
    trusted = await raw_request(
        app, "GET", "/secure", headers, client=("10.1.2.3", 443)
    )
    untrusted = await raw_request(app, "GET", "/secure", headers)
    trusted_headers = dict(trusted["headers"])
    untrusted_headers = dict(untrusted["headers"])
    assert trusted_headers[b"strict-transport-security"] == (
        b"max-age=31536000; includeSubDomains; preload"
    )
    assert b"strict-transport-security" not in untrusted_headers


def test_wsgi_adapter_applies_redirect_and_security_headers() -> None:
    adapter = WSGIAdapter(
        Hinoki(settings=configured(redirect_https=True), include=[secure])
    )
    seen: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        seen["status"], seen["headers"] = status, dict(headers)

    try:
        body = b"".join(
            adapter(
                {
                    "REQUEST_METHOD": "GET",
                    "PATH_INFO": "/secure",
                    "QUERY_STRING": "q=1",
                    "HTTP_HOST": "api.example.test",
                    "SERVER_NAME": "api.example.test",
                    "SERVER_PORT": "80",
                    "wsgi.url_scheme": "http",
                    "wsgi.input": io.BytesIO(b""),
                },
                start_response,
            )
        )
    finally:
        adapter.close()
    assert seen["status"].startswith("308 ")
    assert seen["headers"]["location"] == "https://api.example.test/secure?q=1"
    assert seen["headers"]["x-frame-options"] == "SAMEORIGIN"
    assert body == b""
