"""The native response-header assembly keeps the behaviour of the Python it replaced."""

from __future__ import annotations

import pytest

from hinoki import core
from hinoki.runtime.security import POLICY_HEADERS

RID = b"x-request-id"
ALLOW = b"access-control-allow-origin"
POLICY = [(b"x-frame-options", b"DENY"), (b"referrer-policy", b"no-referrer")]


def reference(
    headers: list[tuple[bytes, bytes]],
    policy: list[tuple[bytes, bytes]],
    route_headers: list[tuple[str, str]],
    rid: bytes,
) -> tuple[list[tuple[bytes, bytes]], bool]:
    """The assembly as the adapter did it in Python before it moved to Rust."""
    merged = [(n, v) for n, v in headers if n.lower() not in POLICY_HEADERS]
    merged.extend(policy)
    present = {name.lower() for name, _ in merged}
    extra = [
        (name.encode(), value.encode())
        for name, value in route_headers
        if name.encode().lower() not in present
        and name.encode().lower() not in POLICY_HEADERS
    ]
    final = [(n, v) for n, v in [*merged, *extra] if n.lower() != RID]
    final.append((RID, rid))
    return final, ALLOW in present


CASES = {
    "plain": ([(b"content-type", b"application/json")], POLICY, [], b"abc"),
    "handler sets a policy header": (
        [(b"X-Frame-Options", b"ALLOWALL"), (b"content-type", b"text/plain")],
        POLICY,
        [],
        b"abc",
    ),
    "handler sets the request id": (
        [(b"X-Request-Id", b"mine"), (b"x-a", b"1")],
        POLICY,
        [],
        b"abc",
    ),
    "route headers fill gaps only": (
        [(b"x-a", b"1")],
        POLICY,
        [("X-A", "route"), ("x-b", "2"), ("x-b", "again")],
        b"abc",
    ),
    "route header cannot set policy or request id": (
        [],
        POLICY,
        [("x-frame-options", "ALLOWALL"), ("x-request-id", "mine"), ("x-c", "3")],
        b"abc",
    ),
    "route header cannot override a policy header the settings left out": (
        [],
        [],
        [("strict-transport-security", "max-age=1"), ("x-c", "3")],
        b"abc",
    ),
    "handler sets allow-origin": (
        [(b"Access-Control-Allow-Origin", b"*")],
        POLICY,
        [],
        b"abc",
    ),
    "route sets allow-origin": (
        [],
        POLICY,
        [("access-control-allow-origin", "https://x")],
        b"abc",
    ),
    "no policy and no route headers": ([(b"x-a", b"1")], [], [], b"abc"),
}


@pytest.mark.parametrize("name", CASES)
def test_response_headers_match_the_python_it_replaced(name: str) -> None:
    headers, policy, route_headers, rid = CASES[name]
    got = core.response_headers(
        list(headers), list(policy), tuple(route_headers), RID, rid, ALLOW
    )
    assert got == reference(headers, policy, route_headers, rid)


def test_response_headers_accept_lists_as_pairs() -> None:
    got, _ = core.response_headers([[b"x-a", b"1"]], [], (), RID, b"r", ALLOW)  # type: ignore[list-item]
    assert got == [(b"x-a", b"1"), (RID, b"r")]


def test_response_headers_reject_a_malformed_pair() -> None:
    with pytest.raises(ValueError, match="pair"):
        core.response_headers([(b"x-a",)], [], (), RID, b"r", ALLOW)  # type: ignore[list-item]


def test_native_policy_names_are_the_python_ones() -> None:
    """The Rust list of policy header names must equal ``POLICY_HEADERS``."""
    for name in POLICY_HEADERS:
        got, _ = core.response_headers([(name.upper(), b"x")], [], (), RID, b"r", ALLOW)
        assert got == [(RID, b"r")], name
        got, _ = core.response_headers([], [], [(name.decode(), "x")], RID, b"r", ALLOW)
        assert got == [(RID, b"r")], name
    got, _ = core.response_headers([(b"x-other", b"x")], [], (), RID, b"r", ALLOW)
    assert got == [(b"x-other", b"x"), (RID, b"r")]


def test_request_headers_returns_the_dict_and_counts_name_value_and_four() -> None:
    assert core.request_headers([]) == ({}, 0)
    got, size = core.request_headers([(b"Ab", b"cde"), [b"f", b""]])
    assert got == {"ab": "cde", "f": ""}
    assert size == 5 + 4 + 1 + 4


def test_request_headers_rejects_what_is_not_a_pair_of_bytes() -> None:
    with pytest.raises(ValueError, match="pair"):
        core.request_headers([(b"a",)])
    with pytest.raises(TypeError):
        core.request_headers([("a", "b")])  # type: ignore[list-item]


def test_header_dict_handles_non_ascii_values_and_repeats() -> None:
    got = core.header_dict(
        [(b"Cookie", b"a=1"), (b"X-\xc9", b"\xe9"), (b"cookie", b"b=2"), (b"x-a", b"1")]
    )
    assert got == {"cookie": "a=1; b=2", "x-é": "é", "x-a": "1"}
