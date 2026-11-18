"""Dependency-free metrics helpers for the generated CRUD orientation run."""

from typing import Any


def request_count(result: dict[str, Any]) -> int:
    """Return the number of HTTP requests represented in an Oha result."""
    return sum(int(count) for count in result["statusCodeDistribution"].values())
