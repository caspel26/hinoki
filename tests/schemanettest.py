"""``ipaddress`` fields of a ``Schema``: validation on both lanes, constructed
values, registration errors, OpenAPI, unions and the request lanes."""

from __future__ import annotations

import random
from collections.abc import Callable
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from typing import Annotated, Any

import pytest
from openapi_spec_validator import validate

from hinoki import Field, Hinoki, Schema, http
from hinoki.errors import RegistrationError, ValidationError
from hinoki.runtime.rustserver import RustWorker
from hinoki.testing import ASGIClient
from tests.rustservertest import JSON, fetch
from tests.schemadifftest import native, oracle, outcome
from tests.xmlnativelanetest import recording


class Host(Schema):
    v4: IPv4Address
    v6: IPv6Address
    net4: IPv4Network
    net6: IPv6Network
    any_ip: IPv4Address | IPv6Address | None = None
    peers: list[IPv4Address] = Field(default_factory=list)


GOOD = {
    "v4": "192.168.0.1",
    "v6": "2001:db8::1",
    "net4": "10.0.0.0/8",
    "net6": "2001:db8::/32",
}


def errors(**changes: object) -> list[tuple[str, str, str]]:
    with pytest.raises(ValidationError) as caught:
        Host.validate({**GOOD, **changes})
    return [(d.path, d.code, d.message) for d in caught.value.details]


def test_addresses_and_networks_are_built_as_ipaddress_objects() -> None:
    host = Host.validate(
        {**GOOD, "any_ip": "::ffff:1.2.3.4", "peers": ["0.0.0.0", "255.255.255.255"]}
    )
    assert host.v4 == IPv4Address("192.168.0.1")
    assert host.v6 == IPv6Address("2001:db8::1")
    assert host.net4 == IPv4Network("10.0.0.0/8")
    assert host.net6 == IPv6Network("2001:db8::/32")
    assert host.any_ip == IPv6Address("::ffff:1.2.3.4")
    assert host.peers == [IPv4Address("0.0.0.0"), IPv4Address("255.255.255.255")]
    assert isinstance(host.v4, IPv4Address) and isinstance(host.net6, IPv6Network)


def test_a_union_takes_the_family_that_parses() -> None:
    assert Host.validate({**GOOD, "any_ip": "10.1.2.3"}).any_ip == IPv4Address(
        "10.1.2.3"
    )
    assert Host.validate({**GOOD, "any_ip": None}).any_ip is None


def test_a_bare_address_is_a_single_host_network() -> None:
    assert Host.validate({**GOOD, "net4": "10.1.2.3"}).net4 == IPv4Network(
        "10.1.2.3/32"
    )
    assert Host.validate({**GOOD, "net6": "::1"}).net6 == IPv6Network("::1/128")


@pytest.mark.parametrize(
    "text",
    [
        "", " 1.2.3.4", "1.2.3.4 ", "1.2.3", "1.2.3.4.5", "256.1.1.1", "01.2.3.4",
        "1.2.3.-4", "1.2.3.4/24", "\uff11.2.3.4", "a.b.c.d", "1..3.4", "0x1.2.3.4",
        "::1", "1.2.3.4\n",
    ],
)  # fmt: skip
def test_malformed_ipv4_is_refused(text: str) -> None:
    assert errors(v4=text) == [("v4", "ipv4", "expected an IPv4 address")]


@pytest.mark.parametrize(
    "text",
    [
        "", "1.2.3.4", "::g", "1::2::3", "12345::1", "fe80::1%eth0", ":1", "1:",
        "1:2:3:4:5:6:7:8:9", "[::1]", "::1/64", " ::1",
    ],
)  # fmt: skip
def test_malformed_ipv6_is_refused(text: str) -> None:
    assert errors(v6=text) == [("v6", "ipv6", "expected an IPv6 address")]


@pytest.mark.parametrize(
    "text",
    [
        "10.0.0.1/24", "10.0.0.0/33", "10.0.0.0/", "10.0.0.0/-1", "10.0.0.0/255.0.0.0",
        "10.0.0.0/0.255.255.255", "10.0.0/8", "/8", "10.0.0.0/8/8", "10.0.0.0/ 8",
    ],
)  # fmt: skip
def test_malformed_ipv4_networks_are_refused(text: str) -> None:
    assert errors(net4=text) == [
        ("net4", "ipv4network", "expected an IPv4 network with no host bits")
    ]


@pytest.mark.parametrize(
    "text", ["2001:db8::1/32", "::/129", "::/", "2001:db8::/ffff::", "fe80::1%1/64"]
)
def test_malformed_ipv6_networks_are_refused(text: str) -> None:
    assert errors(net6=text) == [
        ("net6", "ipv6network", "expected an IPv6 network with no host bits")
    ]


@pytest.mark.parametrize("value", [None, 1, 1.5, True, ["1.2.3.4"], {"a": 1}])
def test_only_text_is_an_address(value: object) -> None:
    assert errors(v4=value)[0][:2] in {("v4", "ipv4"), ("v4", "null")}


def test_constructed_values_are_checked() -> None:
    host = Host(
        v4=IPv4Address("1.2.3.4"),
        v6=IPv6Address("::1"),
        net4=IPv4Network("10.0.0.0/8"),
        net6=IPv6Network("::/0"),
    )
    assert host.v4 == IPv4Address("1.2.3.4")
    for field, wrong in [
        ("v4", IPv6Address("::1")),
        ("v4", "1.2.3.4"),
        ("v6", IPv4Address("1.2.3.4")),
        ("net4", IPv4Address("1.2.3.4")),
        ("net6", IPv4Network("10.0.0.0/8")),
    ]:
        good: dict[str, Any] = {
            "v4": IPv4Address("1.2.3.4"),
            "v6": IPv6Address("::1"),
            "net4": IPv4Network("10.0.0.0/8"),
            "net6": IPv6Network("::/0"),
        }
        good[field] = wrong
        with pytest.raises(ValidationError):
            Host(**good)


def test_dumps_to_text() -> None:
    host = Host.validate({**GOOD, "peers": ["1.1.1.1"]})
    assert host.dump() == {
        **GOOD,
        "any_ip": None,
        "peers": ["1.1.1.1"],
    }


@pytest.mark.parametrize(
    "annotation",
    [
        Annotated[IPv4Address, Field(max_length=4)],
        Annotated[IPv6Network, Field(pattern="x")],
        Annotated[IPv4Address, Field(ge=1)],
    ],
)
def test_constraints_do_not_apply(annotation: Any) -> None:
    with pytest.raises(RegistrationError, match="do not apply"):
        type("Bad", (Schema,), {"__annotations__": {"a": annotation}})


@http.post("/net/json")
async def json_host(data: Host) -> Host:
    return data


def test_openapi_describes_each_family() -> None:
    app = Hinoki()
    app.include(json_host)
    document = app.openapi()
    validate(document)
    props = document["components"]["schemas"]["Host"]["properties"]
    assert props["v4"] == {"type": "string", "format": "ipv4"}
    assert props["v6"] == {"type": "string", "format": "ipv6"}
    assert props["net4"] == {"type": "string", "format": "ipv4network"}
    assert props["net6"] == {"type": "string", "format": "ipv6network"}
    assert props["peers"]["items"] == {"type": "string", "format": "ipv4"}


def test_the_oracle_agrees_on_random_inputs() -> None:
    rng = random.Random(11)  # noqa: S311 - a test fixture, not security
    octets = ["0", "1", "9", "10", "99", "100", "255", "256", "01", "00", "", "x", "-1"]
    groups = ["0", "1", "ffff", "FFFF", "12345", "g", "", "0:0", "db8", "1.2.3.4"]
    prefixes = [
        "0",
        "1",
        "8",
        "24",
        "32",
        "33",
        "08",
        "",
        "-1",
        "x",
        "255.0.0.0",
        "128",
        "129",
    ]

    def v4() -> str:
        return ".".join(rng.choice(octets) for _ in range(rng.choice([3, 4, 4, 4, 5])))

    def v6() -> str:
        text = ":".join(rng.choice(groups) for _ in range(rng.randint(1, 9)))
        shapes = [text, "::" + text, text + "::", text.replace(":0:", "::", 1)]
        return str(rng.choice(shapes))

    def net(maker: Callable[[], str]) -> str:
        return maker() + rng.choice(["", "/" + rng.choice(prefixes)])

    for _ in range(1500):
        data = {
            "v4": rng.choice([v4(), v4(), v6(), 5, None]),
            "v6": rng.choice([v6(), v6(), v4(), "::ffff:" + v4(), None]),
            "net4": net(v4),
            "net6": net(v6),
            "any_ip": rng.choice([v4(), v6(), None]),
            "peers": [v4() for _ in range(rng.randint(0, 2))],
        }
        got = outcome(native, Host, data)
        want = outcome(oracle, Host, data)
        if got[0] == "ok":
            got = ("ok", repr(got[1]))
            want = ("ok", repr(want[1]))
        assert got == want, data


BODIES = [
    b'{"v4": "10.0.0.1", "v6": "::1", "net4": "10.0.0.0/8", "net6": "fe80::/10"}',
    b'{"v4": "10.0.0.256", "v6": "::1", "net4": "10.0.0.0/8", "net6": "fe80::/10"}',
    (
        b'{"v4": "10.0.0.1", "v6": "fe80::1%eth0", '
        b'"net4": "10.0.0.1/8", "net6": "fe80::/10"}'
    ),
    (
        b'{"v4": 1, "v6": "::1", "net4": "10.0.0.0/8", '
        b'"net6": "fe80::/10", "any_ip": "9.9.9.9"}'
    ),
]


async def test_the_native_lane_matches_the_python_lane() -> None:
    app = Hinoki()
    app.include(json_host)
    worker = RustWorker(app, "127.0.0.1", 0)
    lanes: list[str] = []
    pipeline = worker.pipeline
    pipeline.handle_native = recording(lanes, "native", pipeline.handle_native)  # type: ignore[method-assign]
    pipeline.handle = recording(lanes, "python", pipeline.handle)  # type: ignore[method-assign]
    port = await worker.start()
    try:
        served = [
            await fetch(
                port,
                "POST",
                "/net/json",
                JSON + f"content-length: {len(body)}\r\n",
                body=body,
            )
            for body in BODIES
        ]
    finally:
        await worker.stop()
    assert lanes == ["native"] * len(BODIES)
    async with ASGIClient(app) as client:
        for body, got in zip(BODIES, served, strict=True):
            expected = await client.post(
                "/net/json", body=body, headers={"content-type": "application/json"}
            )
            assert got.status == expected.status, body
            if got.status == 200:
                assert got.json() == expected.body, body
            else:
                assert got.json()["details"] == expected.body["details"], body
    assert [reply.status for reply in served] == [200, 422, 422, 422]
