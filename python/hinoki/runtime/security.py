"""Response security headers and an opt-in canonical HTTPS redirect."""

from __future__ import annotations

from urllib.parse import quote, quote_from_bytes

from hinoki.runtime.requests import Scope
from hinoki.runtime.responses import Response
from hinoki.settings import AppSettings, SecuritySettings

FRAME = b"x-frame-options"
REFERRER = b"referrer-policy"
NOSNIFF = b"x-content-type-options"
HSTS = b"strict-transport-security"
POLICY_HEADERS = frozenset({FRAME, REFERRER, NOSNIFF, HSTS})
ASCII_SPACE = 0x20
ASCII_DELETE = 0x7F


def headers_for(settings: SecuritySettings, scheme: str) -> list[tuple[bytes, bytes]]:
    """Return the framework-owned policy headers for one effective scheme."""
    headers = [
        (FRAME, settings.frame_options.value.upper().encode("ascii")),
        (REFERRER, settings.referrer_policy.value.encode("ascii")),
        (NOSNIFF, b"nosniff"),
    ]
    if scheme == "https" and settings.hsts_seconds:
        value = f"max-age={settings.hsts_seconds}"
        if settings.hsts_include_subdomains:
            value += "; includeSubDomains"
        if settings.hsts_preload:
            value += "; preload"
        headers.append((HSTS, value.encode("ascii")))
    return headers


def https_redirect(
    scope: Scope,
    request_headers: dict[str, str],
    app_settings: AppSettings | None,
) -> Response | None:
    """Build a method-preserving redirect only for known plain HTTP requests."""
    if (
        app_settings is None
        or not app_settings.security.redirect_https
        or scope.get("scheme") != "http"
    ):
        return None
    host = request_headers.get("host", "")
    path = redirect_path(scope)
    # HostPolicy has already validated the host. Refuse malformed scope values
    # too, rather than putting untrusted bytes in a Location response header.
    # A target that is not a path (`OPTIONS *`) names nothing to redirect to.
    if not header_safe(host) or not path.startswith(b"/"):
        return None
    host_bytes = host.encode("ascii")
    if host.endswith(":80"):
        host_bytes = host_bytes[:-3]
    query = quote_from_bytes(
        scope.get("query_string", b""), safe="!$&'()*+,-./:;=?@_%~"
    ).encode("ascii")
    target = b"https://" + host_bytes + path
    if query:
        target += b"?" + query
    return Response(308, b"", [(b"location", target)])


def header_safe(host: str) -> bool:
    """A non-empty value of visible ASCII characters only."""
    return bool(host) and all(ASCII_SPACE < ord(char) < ASCII_DELETE for char in host)


def redirect_path(scope: Scope) -> bytes:
    """The quoted raw request path (``/`` when empty)."""
    raw_path = scope.get("raw_path")
    if raw_path is None:
        path = quote(scope.get("path", "/"), safe="/%:@!$&'()*+,;=-._~").encode("ascii")
    else:
        path = quote_from_bytes(raw_path, safe="/%:@!$&'()*+,;=-._~").encode("ascii")
    return path or b"/"
