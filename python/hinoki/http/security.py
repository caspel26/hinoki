"""Small shared checks for browser request-origin signals."""

from __future__ import annotations

from urllib.parse import SplitResult, urlsplit

CROSS_SITE = frozenset({"cross-site", "same-site"})
DEFAULT_PORTS = {"http": 80, "https": 443}


def same_site_origin(
    origin: str | None,
    fetch_site: str,
    host: str,
    scheme: str,
) -> bool:
    """Compare browser Origin with Host after case and default-port normalization."""
    if fetch_site in CROSS_SITE or origin == "null":
        return False
    if origin is None:
        return True
    try:
        origin_parts = urlsplit(origin)
        host_parts = urlsplit(f"//{host}")
        expected_scheme = scheme.casefold()
        return (
            _valid_origin(origin_parts, expected_scheme)
            and _valid_host(host_parts)
            and _authority(origin_parts, expected_scheme)
            == _authority(host_parts, expected_scheme)
        )
    except ValueError:
        return False


def _valid_origin(parts: SplitResult, scheme: str) -> bool:
    return (
        scheme in DEFAULT_PORTS
        and parts.scheme.casefold() == scheme
        and parts.hostname is not None
        and parts.username is None
        and parts.password is None
        and parts.path in {"", "/"}
        and not parts.query
        and not parts.fragment
    )


def _authority(parts: SplitResult, scheme: str) -> tuple[str, int]:
    hostname = parts.hostname
    if hostname is None:
        raise ValueError("host has no name")
    port = parts.port
    return hostname.casefold(), DEFAULT_PORTS[scheme] if port is None else port


def _valid_host(parts: SplitResult) -> bool:
    return (
        parts.hostname is not None
        and parts.username is None
        and parts.password is None
        and not parts.path
        and not parts.query
        and not parts.fragment
    )
