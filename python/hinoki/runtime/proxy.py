"""Resolve proxy metadata only when the immediate peer is explicitly trusted."""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Iterable, MutableMapping
from typing import Any

Scope = MutableMapping[str, Any]
MAX_FORWARDED_HOPS = 32
MAX_REQUEST_ID_BYTES = 128
VISIBLE_ASCII_MIN = 0x21
VISIBLE_ASCII_MAX = 0x7E
COMMA = ord(",")
log = logging.getLogger("hinoki.runtime")


def trusted_peer(
    scope: Scope, networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
) -> bool:
    """Whether the ASGI socket peer belongs to a configured proxy network."""
    client = scope.get("client")
    if not client or not client[0]:
        return False
    try:
        address = ipaddress.ip_address(client[0])
    except ValueError:
        return False
    return any(
        address.version == network.version and address in network
        for network in networks
    )


def effective_scope(
    scope: Scope,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> tuple[Scope, bool]:
    """Copy a scope and apply valid forwarded proto/client values from a trusted peer.

    Host and all headers are preserved. Forwarded values are accepted only once;
    malformed or ambiguous values leave their corresponding socket value intact.
    """
    if not networks or not trusted_peer(scope, networks):
        return scope, False
    headers: Iterable[tuple[bytes, bytes]] = scope.get("headers", ())
    proto_values = [
        value for name, value in headers if name.lower() == b"x-forwarded-proto"
    ]
    resolved: Scope = dict(scope)
    proto = forwarded_proto(proto_values)
    if proto is not None:
        resolved["scheme"] = proto
    resolved["client"] = trusted_client(scope["client"], headers, networks)
    return resolved, True


def effective_client(
    client: tuple[str, int] | None,
    headers: Iterable[tuple[bytes, bytes]],
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> tuple[str, int] | None:
    """The client ``effective_scope`` would resolve for this socket peer and
    these headers, for a transport that has no scope to copy."""
    if client is None or not trusted_peer({"client": client}, networks):
        return client
    return trusted_client(client, headers, networks)


def trusted_client(
    peer: tuple[str, int],
    headers: Iterable[tuple[bytes, bytes]],
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> tuple[str, int]:
    """The forwarded client of a trusted peer, or the peer on ambiguity."""
    for_values = [
        value for name, value in headers if name.lower() == b"x-forwarded-for"
    ]
    client = forwarded_address(for_values, networks, peer[0])
    return peer if client is None else (client, 0)


def forwarded_proto(values: list[bytes]) -> str | None:
    """One valid forwarded protocol value, or the socket scheme on ambiguity."""
    if not values:
        return None
    if len(values) != 1:
        log.warning("ignored repeated X-Forwarded-Proto from trusted proxy")
        return None
    try:
        proto = values[0].decode("ascii").strip().lower()
    except UnicodeDecodeError:
        proto = ""
    if proto not in {"http", "https"}:
        log.warning("ignored invalid X-Forwarded-Proto from trusted proxy")
        return None
    return proto


def forwarded_address(
    values: list[bytes],
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    peer: str,
) -> str | None:
    """One valid forwarded client chain, or the socket peer on ambiguity."""
    if not values:
        return None
    if len(values) != 1:
        log.warning("ignored repeated X-Forwarded-For from trusted proxy")
        return None
    client = forwarded_client(values[0], networks, peer)
    if client is None:
        log.warning("ignored invalid X-Forwarded-For from trusted proxy")
    return client


def forwarded_client(
    raw: bytes,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    peer: str,
) -> str | None:
    """Walk an X-Forwarded-For chain from the trusted peer toward the client."""
    try:
        values = raw.decode("ascii").split(",")
        if not 1 <= len(values) <= MAX_FORWARDED_HOPS:
            return None
        chain = [ipaddress.ip_address(value.strip()) for value in values]
        current = ipaddress.ip_address(peer)
    except (UnicodeDecodeError, ValueError):
        return None
    for address in reversed(chain):
        if not is_trusted(current, networks):
            break
        current = address
    if is_trusted(current, networks):
        return None
    return str(current)


def is_trusted(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    return any(
        address.version == network.version and address in network
        for network in networks
    )


def inbound_request_id(headers: list[tuple[bytes, bytes]], name: str) -> str | None:
    """Accept one bounded, visible ASCII request ID; reject ambiguous values."""
    found = [value for key, value in headers if key.lower() == name.encode("ascii")]
    if len(found) != 1 or not 1 <= len(found[0]) <= MAX_REQUEST_ID_BYTES:
        return None
    value = found[0]
    if any(
        byte < VISIBLE_ASCII_MIN or byte > VISIBLE_ASCII_MAX or byte == COMMA
        for byte in value
    ):
        return None
    return value.decode("ascii")
