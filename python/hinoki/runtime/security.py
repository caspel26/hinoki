"""Response security headers and an opt-in canonical HTTPS redirect.

Header assembly and redirect URL construction delegate to Rust
(``hinoki.core.security_headers`` and ``hinoki.core.redirect_location``, backed
by ``kiso::redirect``) so Kiso and Granian share one implementation.
"""

from __future__ import annotations

from typing import Any, Protocol, cast

from hinoki import core
from hinoki.runtime.requests import Scope
from hinoki.runtime.responses import Response
from hinoki.settings import AppSettings, SecuritySettings

FRAME = b"x-frame-options"
REFERRER = b"referrer-policy"
NOSNIFF = b"x-content-type-options"
HSTS = b"strict-transport-security"
POLICY_HEADERS = frozenset({FRAME, REFERRER, NOSNIFF, HSTS})


class _RedirectLocationFn(Protocol):
    def __call__(self, host: str, path: bytes, query: bytes) -> bytes | None: ...


class _HeaderSafeFn(Protocol):
    def __call__(self, host: str) -> bool: ...


class _SecurityHeadersFn(Protocol):
    def __call__(
        self,
        frame_options: str,
        referrer_policy: str,
        hsts_seconds: int,
        hsts_include_subdomains: bool,
        hsts_preload: bool,
        scheme: str,
    ) -> list[tuple[bytes, bytes]]: ...


_native = cast("Any", core)
_redirect_location = cast("_RedirectLocationFn", _native.redirect_location)
_header_safe = cast("_HeaderSafeFn", _native.header_safe)
_security_headers = cast("_SecurityHeadersFn", _native.security_headers)


def headers_for(settings: SecuritySettings, scheme: str) -> list[tuple[bytes, bytes]]:
    """Return the framework-owned policy headers for one effective scheme."""
    return _security_headers(
        settings.frame_options.value,
        settings.referrer_policy.value,
        settings.hsts_seconds,
        settings.hsts_include_subdomains,
        settings.hsts_preload,
        scheme,
    )


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
    raw_path = scope.get("raw_path")
    path_bytes = (
        scope.get("path", "/").encode("utf-8") if raw_path is None else raw_path
    )
    query: bytes = scope.get("query_string", b"")
    target = _redirect_location(host, path_bytes, query)
    if target is None:
        return None
    return Response(308, b"", [(b"location", target)])


def header_safe(host: str) -> bool:
    """A non-empty value of visible ASCII characters only."""
    return _header_safe(host)
