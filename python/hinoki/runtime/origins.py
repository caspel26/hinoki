"""Host and cross-origin policies, built once per worker from the settings.

Both are ``None`` when the settings leave them inert (any host, no CORS
origins), so a request then pays one identity test for each. Evaluation is
delegated to Rust (``hinoki.core.HostPolicy`` and ``hinoki.core.CorsPolicy``,
backed by ``kiso``) so Kiso and Granian share one implementation.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, Protocol, cast

from hinoki import core
from hinoki.runtime.responses import Headers, HTTPError, Response
from hinoki.settings import ANY, AppSettings, CorsSettings

PREFLIGHT_VARY = b"access-control-request-method, access-control-request-headers"
VARY_ORIGIN = (b"vary", b"origin")
ALLOW_ORIGIN = b"access-control-allow-origin"

# A lifespan or request scope key set by in-process drivers (the test clients,
# the MCP scenario runner): no network Host header reaches them.
IN_PROCESS = "hinoki.in_process"

# What the host policy accepts when ``debug`` is on, ``allowed_hosts`` is ``*``
# and the app serves a feature that decides same-origin from the Host header:
# ``localhost`` and its subdomains, the whole 127.0.0.0/8 block (the entry the
# Rust matcher reads as that block) and the IPv6 loopback address.
LOOPBACK_HOSTS = ("localhost", ".localhost", "127.0.0.0/8", "::1")


def host_trusting_feature(app: Any) -> str | None:
    """What the app serves that decides same-origin from the Host header."""
    if app.has_admin:
        return "the admin"
    return "WebSocket routes" if app.websockets else None


class _HostNameFn(Protocol):
    def __call__(self, value: str) -> str: ...


class _NativeHostPolicy(Protocol):
    def __init__(self, allowed_hosts: list[str]) -> None: ...
    def allows(self, header: str) -> bool: ...
    @property
    def exact(self) -> list[str]: ...
    @property
    def suffixes(self) -> list[str]: ...


class _NativeCorsPolicy(Protocol):
    def __init__(
        self,
        origins: list[str],
        any_origin: bool,
        credentials: bool,
        expose: bytes,
        preflight: Headers,
    ) -> None: ...
    def allowed(self, origin: str) -> bool: ...
    def headers(self, origin: str) -> Headers: ...
    def headers_from_scope(
        self, raw_headers: Iterable[tuple[bytes, bytes]]
    ) -> Headers: ...
    def preflight(
        self, method: str, origin: str | None, requests_method: bool
    ) -> tuple[bool, Headers | None]: ...


_native = cast("Any", core)
_host_name = cast("_HostNameFn", _native.host_name)
_HostPolicyCls = cast("type[_NativeHostPolicy]", _native.HostPolicy)
_CorsPolicyCls = cast("type[_NativeCorsPolicy]", _native.CorsPolicy)


def host_name(value: str) -> str:
    """The host of a ``Host`` header: lower-case, without port, IPv6 brackets or
    a trailing dot. Empty when the header is missing or is anything but one
    host with an optional port (so two joined ``Host`` headers never match)."""
    return _host_name(value)


@dataclass(frozen=True, slots=True)
class HostPolicy:
    """The hosts a request may be addressed to."""

    exact: frozenset[str]
    suffixes: tuple[str, ...]
    _native: _NativeHostPolicy = field(init=False, repr=False, compare=False)
    # True for the loopback-only policy no setting asked for (see ``of``).
    implicit: bool = field(default=False, compare=False)

    def __post_init__(self) -> None:
        native = _HostPolicyCls([*self.exact, *self.suffixes])
        object.__setattr__(self, "_native", native)

    @classmethod
    def of(cls, settings: object, *, host_trusting: bool = False) -> HostPolicy | None:
        """The policy the settings name. With ``allowed_hosts`` left at ``*`` and
        ``debug`` on, an app that serves a ``host_trusting`` feature (the admin,
        WebSocket routes) gets the loopback hosts only, so DNS rebinding cannot
        reach a development server; an explicit list is always used as given."""
        if not isinstance(settings, AppSettings):
            return None
        implicit = ANY in settings.allowed_hosts
        if implicit and not (settings.debug and host_trusting):
            return None
        entries = LOOPBACK_HOSTS if implicit else settings.allowed_hosts
        native = _HostPolicyCls(list(entries))
        policy = object.__new__(cls)
        object.__setattr__(policy, "exact", frozenset(native.exact))
        object.__setattr__(policy, "suffixes", tuple(native.suffixes))
        object.__setattr__(policy, "_native", native)
        object.__setattr__(policy, "implicit", implicit)
        return policy

    def exempts(self, scope: Any) -> bool:
        """Whether an in-process driver's request skips an implicit policy: its
        Host is not a network Host. An explicit policy always applies."""
        return self.implicit and bool(scope.get(IN_PROCESS))

    def allows(self, header: str) -> bool:
        return self._native.allows(header)

    def check(self, header: str) -> None:
        if not self.allows(header):
            raise HTTPError(HTTPStatus.BAD_REQUEST, "disallowed_host")


@dataclass(frozen=True, slots=True)
class CorsPolicy:
    """Answers preflights and names the headers an allowed origin receives."""

    origins: frozenset[str]
    any_origin: bool
    credentials: bool
    expose: bytes
    preflight: Headers
    _native: _NativeCorsPolicy = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        native = _CorsPolicyCls(
            list(self.origins),
            self.any_origin,
            self.credentials,
            self.expose,
            list(self.preflight),
        )
        object.__setattr__(self, "_native", native)

    @classmethod
    def of(cls, settings: object) -> CorsPolicy | None:
        cors = settings.cors if isinstance(settings, AppSettings) else None
        if cors is None or not cors.origins:
            return None
        return cls.from_cors(cors)

    @classmethod
    def from_cors(cls, cors: CorsSettings) -> CorsPolicy:
        """The policy of one set of CORS fields: the application's or a route's."""
        return cls(
            # Browsers send origins in lower case; the comparison is exact.
            frozenset(origin.lower() for origin in cors.origins),
            ANY in cors.origins,
            cors.credentials,
            ", ".join(cors.expose_headers).lower().encode(),
            preflight_headers(cors),
        )

    def allowed(self, origin: str) -> bool:
        return self._native.allowed(origin)

    def headers(self, origin: str) -> Headers:
        """The headers added to a response for a request from ``origin``. With a
        list of origins every answer varies by ``Origin``, also the ones that
        get no permission, so a shared cache keeps them apart."""
        return self._native.headers(origin)

    def headers_from_scope(self, raw_headers: Iterable[tuple[bytes, bytes]]) -> Headers:
        """``headers`` computed directly from raw ASGI scope headers in Rust."""
        return self._native.headers_from_scope(raw_headers)

    def answer(self, method: str, headers: dict[str, str]) -> Response | None:
        """The answer to a preflight request, or ``None`` for any other request.
        The origin headers are added with the other responses' headers."""
        denied, preflight = self._native.preflight(
            method,
            headers.get("origin"),
            "access-control-request-method" in headers,
        )
        if denied:
            raise HTTPError(HTTPStatus.FORBIDDEN, "cors_origin_denied")
        if preflight is None:
            return None
        return Response(HTTPStatus.NO_CONTENT, b"", preflight)


def preflight_headers(cors: CorsSettings) -> Headers:
    methods = ", ".join(method.upper() for method in cors.methods)
    allowed = ", ".join(header.lower() for header in cors.headers)
    return [
        (b"access-control-allow-methods", methods.encode()),
        (b"access-control-allow-headers", allowed.encode()),
        (b"access-control-max-age", str(cors.max_age).encode()),
        (b"vary", PREFLIGHT_VARY),
    ]
