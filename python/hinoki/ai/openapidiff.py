"""Detect changes between two OpenAPI documents that break existing clients."""

from __future__ import annotations

from typing import Any

from hinoki import core


def breaking_changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Problems an existing client of ``old`` meets against ``new``.

    Additions are compatible. Removed operations or success responses, new or newly
    required request input, removed response properties, changed types and narrowed
    request or widened response enums are reported."""
    return core.openapi_breaking_changes(core.encode_json(old), core.encode_json(new))
