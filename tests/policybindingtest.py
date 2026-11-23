"""Parity and edge-case tests for Rust-backed request-path policy bindings."""

from __future__ import annotations

import ipaddress
import logging
from typing import Any, cast

import pytest

from hinoki import core
from hinoki.runtime.origins import CorsPolicy, HostPolicy, host_name
from hinoki.runtime.proxy import (
    build_proxy_policy,
    effective_client,
    effective_scope,
    forwarded_client,
    inbound_request_id,
    inbound_request_id_checked,
    trusted_client,
    trusted_peer,
)
from hinoki.runtime.security import header_safe, headers_for, https_redirect
from hinoki.settings import (
    AppSettings,
    CorsSettings,
    FrameOptions,
    ReferrerPolicy,
    SecuritySettings,
)


def test_proxy_policy_rejects_invalid_cidr() -> None:
    with pytest.raises(ValueError, match="invalid trusted proxy"):
        build_proxy_policy(["10.0.0.0/33"])


def test_proxy_policy_ipv4_ipv6_and_xff_boundaries(
    caplog: pytest.LogCaptureFixture,
) -> None:
    policy = build_proxy_policy(("10.0.0.0/8", "2001:db8::/32"))
    assert trusted_peer({"client": ("10.1.2.3", 80)}, policy) is True
    assert trusted_peer({"client": ("2001:db8::1", 80)}, policy) is True
    assert trusted_peer({"client": ("::ffff:10.1.2.3", 80)}, policy) is False
    assert trusted_peer({"client": None}, policy) is False

    # 32 hops accepted, 33 rejected.
    ok_hops = b", ".join([b"203.0.113.5", *([b"10.0.0.1"] * 31)])
    too_many = b", ".join([b"203.0.113.5", *([b"10.0.0.1"] * 32)])
    assert forwarded_client(ok_hops, policy, "10.0.0.2") == "203.0.113.5"
    assert forwarded_client(too_many, policy, "10.0.0.2") is None

    # Repeated headers leave socket metadata untouched and log warnings.
    scope: dict[str, Any] = {
        "client": ("10.1.2.3", 443),
        "scheme": "http",
        "headers": [
            (b"X-Forwarded-Proto", b"https"),
            (b"x-forwarded-proto", b"http"),
            (b"X-Forwarded-For", b"198.51.100.1"),
            (b"x-forwarded-for", b"198.51.100.2"),
        ],
    }
    with caplog.at_level(logging.WARNING, logger="hinoki.runtime"):
        resolved, trusted = effective_scope(scope, policy)
    assert trusted is True
    assert resolved["scheme"] == "http"
    assert resolved["client"] == ("10.1.2.3", 443)
    assert "ignored repeated X-Forwarded-Proto" in caplog.text
    assert "ignored repeated X-Forwarded-For" in caplog.text

    # effective_client and trusted_client work with both NativeProxyPolicy and tuple.
    headers = [(b"X-Forwarded-For", b"198.51.100.9, 10.2.3.4")]
    assert effective_client(("10.1.2.3", 443), headers, policy) == ("198.51.100.9", 0)
    assert effective_client(("192.0.2.1", 443), headers, policy) == ("192.0.2.1", 443)
    nets = (ipaddress.ip_network("10.0.0.0/8"),)
    assert trusted_client(("10.1.2.3", 443), headers, nets) == ("198.51.100.9", 0)


def test_inbound_request_id_checked_single_pass_flag() -> None:
    assert inbound_request_id_checked([], b"x-request-id") == (None, False)
    assert inbound_request_id_checked(
        [(b"X-Request-ID", b"req-1")], b"x-request-id"
    ) == (
        "req-1",
        True,
    )
    assert inbound_request_id_checked(
        [(b"x-request-id", b"bad id")], b"x-request-id"
    ) == (
        None,
        True,
    )
    assert (
        inbound_request_id([(b"x-request-id", b"a" * 128)], "x-request-id") == "a" * 128
    )
    assert inbound_request_id([(b"x-request-id", b"a" * 129)], "x-request-id") is None


def test_host_policy_and_host_name_parity() -> None:
    assert host_name("EXAMPLE.COM:443") == "example.com"
    assert host_name("[2001:0db8::1]:8443") == "2001:db8::1"
    assert host_name("example.com, example.com") == ""

    settings = AppSettings(
        allowed_hosts=("example.com", ".example.org", "2001:0db8::1")
    )
    policy = HostPolicy.of(settings)
    assert policy is not None
    assert policy.allows("example.com.") is True
    assert policy.allows("sub.example.org:8080") is True
    assert policy.allows("example.org") is True
    assert policy.allows("[2001:db8::1]") is True
    assert policy.allows("aexample.org") is False
    assert policy.allows("example.com, example.com") is False

    # Direct dataclass construction also wires the Rust evaluator.
    direct = HostPolicy(frozenset({"api.test"}), (".svc.test",))
    assert direct.allows("api.test:80") is True
    assert direct.allows("a.svc.test") is True
    assert direct.allows("other.test") is False


def test_cors_policy_headers_and_scope_headers_parity() -> None:
    cors = CorsPolicy.from_cors(
        CorsSettings(
            origins=("https://app.example",),
            credentials=True,
            expose_headers=("X-Total",),
        )
    )
    assert cors.allowed("https://app.example") is True
    assert cors.allowed("https://evil.example") is False

    expected = cors.headers("https://app.example")
    assert cors.headers_from_scope([(b"origin", b"https://app.example")]) == expected
    assert cors.headers_from_scope(
        [(b"origin", b"https://app.example"), (b"origin", b"https://app.example")]
    ) == [(b"vary", b"origin")]
    assert cors.headers_from_scope([]) == [(b"vary", b"origin")]

    # Direct native class exists on hinoki.core.
    native = cast("Any", core)
    assert hasattr(native, "ProxyPolicy")
    assert hasattr(native, "HostPolicy")
    assert hasattr(native, "CorsPolicy")


def test_security_headers_and_redirect_parity() -> None:
    sec = SecuritySettings(
        frame_options=FrameOptions.SAMEORIGIN,
        referrer_policy=ReferrerPolicy.NO_REFERRER,
        hsts_seconds=31536000,
        hsts_include_subdomains=True,
        hsts_preload=True,
        redirect_https=True,
    )
    http_headers = headers_for(sec, "http")
    https_headers = headers_for(sec, "https")
    assert (b"x-frame-options", b"SAMEORIGIN") in http_headers
    assert (b"referrer-policy", b"no-referrer") in http_headers
    assert (b"x-content-type-options", b"nosniff") in http_headers
    assert all(name != b"strict-transport-security" for name, _ in http_headers)
    assert (
        b"strict-transport-security",
        b"max-age=31536000; includeSubDomains; preload",
    ) in https_headers

    assert header_safe("example.com:80") is True
    assert header_safe("bad host") is False

    settings = AppSettings(allowed_hosts=("example.com",), security=sec)
    scope: dict[str, Any] = {
        "scheme": "http",
        "path": "/items/a b",
        "raw_path": b"/items/a%2Fb c",
        "query_string": b"q=hello world",
    }
    response = https_redirect(scope, {"host": "example.com:80"}, settings)
    assert response is not None
    assert response.status == 308
    assert response.headers == [
        (b"location", b"https://example.com/items/a%2Fb%20c?q=hello%20world")
    ]
