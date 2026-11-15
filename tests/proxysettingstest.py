"""Trusted socket-peer and forwarded-address parsing helpers."""

import ipaddress
from typing import Any

import pytest

from hinoki.runtime.proxy import effective_scope, forwarded_client, inbound_request_id

NETWORKS = (ipaddress.ip_network("10.0.0.0/8"),)


def test_proxy_peer_requires_a_parseable_client_address() -> None:
    scope: dict[str, Any] = {"client": None, "headers": [], "scheme": "http"}
    assert effective_scope(scope, NETWORKS) == (scope, False)
    scope["client"] = ("not-an-ip", 1234)
    assert effective_scope(scope, NETWORKS) == (scope, False)


def test_forwarded_proto_requires_one_ascii_http_token() -> None:
    scope = {
        "client": ("10.1.2.3", 443),
        "headers": [(b"x-forwarded-proto", b"https\xff")],
        "scheme": "http",
    }
    resolved, trusted = effective_scope(scope, NETWORKS)
    assert trusted is True
    assert resolved["scheme"] == "http"


def test_forwarded_for_is_bounded_and_stops_at_first_untrusted_hop() -> None:
    many_hops = b",".join([b"10.1.2.3"] * 33)
    assert forwarded_client(many_hops, NETWORKS, "10.2.3.4") is None
    assert (
        forwarded_client(
            b"198.51.100.9, 203.0.113.8, 10.1.2.3",
            NETWORKS,
            "10.2.3.4",
        )
        == "203.0.113.8"
    )


@pytest.mark.parametrize("header", [b"", b"a" * 129, b"has space", b"two,values"])
def test_invalid_inbound_request_id_is_not_accepted(header: bytes) -> None:
    assert inbound_request_id([(b"x-request-id", header)], "x-request-id") is None
