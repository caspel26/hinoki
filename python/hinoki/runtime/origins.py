"""Host and cross-origin policies, built once per worker from the settings.

Both are ``None`` when the settings leave them inert (any host, no CORS
origins), so a request then pays one identity test for each.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from http import HTTPStatus

from hinoki.runtime.responses import Headers, HTTPError, Response
from hinoki.settings import ANY, AppSettings, CorsSettings

PREFLIGHT_VARY = b"access-control-request-method, access-control-request-headers"
VARY_ORIGIN = (b"vary", b"origin")
ALLOW_ORIGIN = b"access-control-allow-origin"
LABEL = r"[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?"
HOST_HEADER = re.compile(
    rf"(?:\[(?P<address>[0-9a-f:.]+)\]|(?P<name>{LABEL}(?:\.{LABEL})*)\.?)"
    r"(?::[0-9]{1,5})?",
    re.ASCII,
)


def host_name(value: str) -> str:
    """The host of a ``Host`` header: lower-case, without port, IPv6 brackets or
    a trailing dot. Empty when the header is missing or is anything but one
    host with an optional port (so two joined ``Host`` headers never match)."""
    found = HOST_HEADER.fullmatch(value.strip().lower())
    if found is None:
        return ""
    address = found["address"]
    return found["name"] if address is None else canonical_address(address)


def canonical_address(text: str) -> str:
    """The compressed spelling of an IPv6 address, or an empty string."""
    try:
        return str(ipaddress.IPv6Address(text))
    except ValueError:
        return ""


def host_entry(entry: str) -> str:
    """An ``allowed_hosts`` entry as ``host_name`` spells the same host."""
    entry = entry.lower()
    return canonical_address(entry) if ":" in entry else entry


@dataclass(frozen=True, slots=True)
class HostPolicy:
    """The hosts a request may be addressed to."""

    exact: frozenset[str]
    suffixes: tuple[str, ...]

    @classmethod
    def of(cls, settings: object) -> HostPolicy | None:
        if not isinstance(settings, AppSettings) or ANY in settings.allowed_hosts:
            return None
        hosts = [host_entry(entry) for entry in settings.allowed_hosts]
        # ``.example.com`` allows the domain itself and every subdomain.
        domains = [entry for entry in hosts if entry.startswith(".")]
        exact = [entry for entry in hosts if not entry.startswith(".")]
        return cls(frozenset([*exact, *(d[1:] for d in domains)]), tuple(domains))

    def allows(self, header: str) -> bool:
        name = host_name(header)
        return bool(name) and (name in self.exact or name.endswith(self.suffixes))

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
        return self.any_origin or origin in self.origins

    def headers(self, origin: str) -> Headers:
        """The headers added to a response for a request from ``origin``. With a
        list of origins every answer varies by ``Origin``, also the ones that
        get no permission, so a shared cache keeps them apart."""
        if self.any_origin:
            head: Headers = [(ALLOW_ORIGIN, b"*")] if origin else []
        elif origin in self.origins:
            head = [(ALLOW_ORIGIN, origin.encode("ascii")), VARY_ORIGIN]
        else:
            return [VARY_ORIGIN]
        if head and self.credentials:
            head.append((b"access-control-allow-credentials", b"true"))
        if head and self.expose:
            head.append((b"access-control-expose-headers", self.expose))
        return head

    def answer(self, method: str, headers: dict[str, str]) -> Response | None:
        """The answer to a preflight request, or ``None`` for any other request.
        The origin headers are added with the other responses' headers."""
        if method != "OPTIONS" or "access-control-request-method" not in headers:
            return None
        origin = headers.get("origin", "")
        if not origin:
            return None
        if not self.allowed(origin):
            raise HTTPError(HTTPStatus.FORBIDDEN, "cors_origin_denied")
        return Response(HTTPStatus.NO_CONTENT, b"", list(self.preflight))


def preflight_headers(cors: CorsSettings) -> Headers:
    methods = ", ".join(method.upper() for method in cors.methods)
    allowed = ", ".join(header.lower() for header in cors.headers)
    return [
        (b"access-control-allow-methods", methods.encode()),
        (b"access-control-allow-headers", allowed.encode()),
        (b"access-control-max-age", str(cors.max_age).encode()),
        (b"vary", PREFLIGHT_VARY),
    ]
