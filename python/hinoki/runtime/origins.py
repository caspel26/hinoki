"""Host and cross-origin policies, built once per worker from the settings.

Both are ``None`` when the settings leave them inert (any host, no CORS
origins), so a request then pays one identity test for each.
"""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus

from hinoki.runtime.responses import Headers, HTTPError, Response
from hinoki.settings import ANY, AppSettings, CorsSettings

PREFLIGHT_VARY = b"access-control-request-method, access-control-request-headers"


def host_name(value: str) -> str:
    """The host of a ``Host`` header: lower-case, without port, IPv6 brackets or
    a trailing dot. Empty when the header is missing or malformed."""
    value = value.strip().lower()
    if value.startswith("["):
        name, bracket, rest = value[1:].partition("]")
        port = rest.removeprefix(":")
        valid = bool(bracket) and (not rest or (rest.startswith(":") and port != ""))
    else:
        name, _, port = value.partition(":")
        valid = ":" not in port
    if not valid or (port and not port.isdigit()):
        return ""
    return name.removesuffix(".")


@dataclass(frozen=True, slots=True)
class HostPolicy:
    """The hosts a request may be addressed to."""

    exact: frozenset[str]
    suffixes: tuple[str, ...]

    @classmethod
    def of(cls, settings: object) -> HostPolicy | None:
        if not isinstance(settings, AppSettings) or ANY in settings.allowed_hosts:
            return None
        hosts = [entry.lower() for entry in settings.allowed_hosts]
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
        return cls(
            # Browsers send origins in lower case; the comparison is exact.
            frozenset(origin.lower() for origin in cors.origins),
            ANY in cors.origins,
            cors.credentials,
            ", ".join(cors.expose_headers).lower().encode(),
            preflight_headers(cors),
        )

    def headers(self, origin: str) -> Headers:
        """The headers added to a response for a request from ``origin``; empty
        when the origin is absent or not allowed."""
        if not origin:
            return []
        if self.any_origin:
            head: Headers = [(b"access-control-allow-origin", b"*")]
        elif origin in self.origins:
            head = [
                (b"access-control-allow-origin", origin.encode("ascii")),
                (b"vary", b"origin"),
            ]
        else:
            return []
        if self.credentials:
            head.append((b"access-control-allow-credentials", b"true"))
        if self.expose:
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
        if not self.headers(origin):
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
