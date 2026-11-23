"""Resolve proxy metadata only when the immediate peer is explicitly trusted.

Parsing and CIDR matching live in Rust (``hinoki.core.ProxyPolicy`` and
``hinoki.core.inbound_request_id``, backed by ``kiso``) so Kiso and Granian
share one policy implementation.
"""

from __future__ import annotations

import functools
import ipaddress
import logging
from collections.abc import Iterable, MutableMapping
from typing import Any, Protocol, cast

from hinoki import core

Scope = MutableMapping[str, Any]
ProxyNetworks = tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
MAX_FORWARDED_HOPS = 32
MAX_REQUEST_ID_BYTES = 128
log = logging.getLogger("hinoki.runtime")


class NativeProxyPolicy(Protocol):
    """Compiled Rust proxy network matcher (``hinoki.core.ProxyPolicy``)."""

    def __init__(self, networks: list[str]) -> None: ...
    def is_empty(self) -> bool: ...
    def trusts(self, peer: str) -> bool: ...
    def forwarded_client(self, raw: bytes, peer: str) -> str | None: ...
    def resolve(
        self, peer: str | None, headers: Iterable[tuple[bytes, bytes]]
    ) -> tuple[bool, str | None, str | None, str | None, str | None]: ...
    def resolve_client(
        self, peer: str | None, headers: Iterable[tuple[bytes, bytes]]
    ) -> tuple[bool, str | None, str | None]: ...


class _InboundRequestId(Protocol):
    def __call__(
        self, headers: Iterable[tuple[bytes, bytes]], name: bytes
    ) -> tuple[str | None, bool]: ...


_native = cast("Any", core)
_ProxyPolicyCls = cast("type[NativeProxyPolicy]", _native.ProxyPolicy)
_inbound_request_id = cast("_InboundRequestId", _native.inbound_request_id)


def build_proxy_policy(networks: ProxyNetworks | Iterable[str]) -> NativeProxyPolicy:
    """Compile trusted proxy networks once for reuse across requests."""
    return _ProxyPolicyCls([str(network) for network in networks])


@functools.lru_cache(maxsize=32)
def _cached_policy(networks: ProxyNetworks) -> NativeProxyPolicy:
    return build_proxy_policy(networks)


def _as_policy(networks: ProxyNetworks | NativeProxyPolicy) -> NativeProxyPolicy:
    if isinstance(networks, tuple):
        return _cached_policy(networks)
    return networks


def _warn_forwarded(header: str, kind: str | None) -> None:
    if kind is not None:
        log.warning("ignored %s %s from trusted proxy", kind, header)


def _peer_host(scope: Scope) -> str | None:
    client = scope.get("client")
    if not client or not client[0]:
        return None
    return str(client[0])


def trusted_peer(scope: Scope, networks: ProxyNetworks | NativeProxyPolicy) -> bool:
    """Whether the ASGI socket peer belongs to a configured proxy network."""
    peer = _peer_host(scope)
    if peer is None:
        return False
    return _as_policy(networks).trusts(peer)


def effective_scope(
    scope: Scope,
    networks: ProxyNetworks | NativeProxyPolicy,
) -> tuple[Scope, bool]:
    """Copy a scope and apply valid forwarded proto/client values from a trusted peer.

    Host and all headers are preserved. Forwarded values are accepted only once;
    malformed or ambiguous values leave their corresponding socket value intact.
    """
    policy = _as_policy(networks)
    if policy.is_empty():
        return scope, False
    peer = _peer_host(scope)
    if peer is None:
        return scope, False
    headers: Iterable[tuple[bytes, bytes]] = scope.get("headers", ())
    trusted, proto, client, proto_warn, for_warn = policy.resolve(peer, headers)
    if not trusted:
        return scope, False
    _warn_forwarded("X-Forwarded-Proto", proto_warn)
    _warn_forwarded("X-Forwarded-For", for_warn)
    resolved: Scope = dict(scope)
    if proto is not None:
        resolved["scheme"] = proto
    if client is not None:
        resolved["client"] = (client, 0)
    return resolved, True


def effective_client(
    client: tuple[str, int] | None,
    headers: Iterable[tuple[bytes, bytes]],
    networks: ProxyNetworks | NativeProxyPolicy,
) -> tuple[str, int] | None:
    """The client ``effective_scope`` would resolve for this socket peer and
    these headers, for a transport that has no scope to copy."""
    if client is None or not client[0]:
        return client
    policy = _as_policy(networks)
    if policy.is_empty():
        return client
    trusted, resolved, for_warn = policy.resolve_client(client[0], headers)
    if not trusted:
        return client
    _warn_forwarded("X-Forwarded-For", for_warn)
    return client if resolved is None else (resolved, 0)


def trusted_client(
    peer: tuple[str, int],
    headers: Iterable[tuple[bytes, bytes]],
    networks: ProxyNetworks | NativeProxyPolicy,
) -> tuple[str, int]:
    """The forwarded client of a trusted peer, or the peer on ambiguity."""
    _, resolved, for_warn = _as_policy(networks).resolve_client(peer[0], headers)
    _warn_forwarded("X-Forwarded-For", for_warn)
    return peer if resolved is None else (resolved, 0)


def forwarded_client(
    raw: bytes,
    networks: ProxyNetworks | NativeProxyPolicy,
    peer: str,
) -> str | None:
    """Walk an X-Forwarded-For chain from the trusted peer toward the client."""
    return _as_policy(networks).forwarded_client(raw, peer)


def inbound_request_id_checked(
    headers: Iterable[tuple[bytes, bytes]], name: bytes
) -> tuple[str | None, bool]:
    """Single-pass request ID check returning ``(valid_id, saw_header)``."""
    return _inbound_request_id(headers, name)


def inbound_request_id(headers: Iterable[tuple[bytes, bytes]], name: str) -> str | None:
    """Accept one bounded, visible ASCII request ID; reject ambiguous values."""
    value, _ = _inbound_request_id(headers, name.encode("ascii"))
    return value
