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
    # HostPolicy has already validated this value. Refuse malformed scope values
    # too, rather than putting untrusted bytes in a Location response header.
    try:
        host_bytes = host.encode("ascii")
    except UnicodeEncodeError:
        return None
    if not host_bytes or any(
        byte <= ASCII_SPACE or byte >= ASCII_DELETE for byte in host_bytes
    ):
        return None
    if host.endswith(":80"):
        host_bytes = host_bytes[:-3]
    raw_path = scope.get("raw_path")
    if raw_path is None:
        path = quote(scope.get("path", "/"), safe="/%:@!$&'()*+,;=-._~").encode("ascii")
    else:
        path = quote_from_bytes(raw_path, safe="/%:@!$&'()*+,;=-._~").encode("ascii")
    query = quote_from_bytes(
        scope.get("query_string", b""), safe="!$&'()*+,-./:;=?@_%~"
    ).encode("ascii")
    target = b"https://" + host_bytes + (path or b"/")
    if query:
        target += b"?" + query
    return Response(308, b"", [(b"location", target)])
