"""Small shared checks for browser request-origin signals."""

from __future__ import annotations

from hinoki import core


def same_site_origin(
    origin: str | None,
    fetch_site: str,
    host: str,
    scheme: str,
) -> bool:
    """Compare browser Origin with Host after case and default-port normalization."""
    return core.same_site_origin(origin, fetch_site, host, scheme)
