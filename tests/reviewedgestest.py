from __future__ import annotations

import ipaddress

import pytest

from hinoki.http.request import media_type
from hinoki.runtime.proxy import is_trusted, trusted_peer


def test_a_quoted_boundary_may_contain_a_comma() -> None:
    raw = 'multipart/form-data; boundary="a,b"'
    assert media_type(raw) == "multipart/form-data"


def test_repeated_content_types_are_still_refused() -> None:
    with pytest.raises(Exception, match="several Content-Type values"):
        media_type("application/json, text/plain")


def test_an_ipv4_mapped_peer_matches_ipv4_proxy_networks() -> None:
    networks = (ipaddress.ip_network("10.0.0.0/8"),)
    assert is_trusted(ipaddress.ip_address("::ffff:10.0.0.1"), networks)
    assert trusted_peer({"client": ("::ffff:10.0.0.1", 1)}, networks)  # type: ignore[typeddict-item]
    assert not trusted_peer({"client": ("::ffff:192.168.0.1", 1)}, networks)  # type: ignore[typeddict-item]
