"""Direct request-scope validation for the HTTPS redirect boundary."""

from __future__ import annotations

from typing import Any

import pytest

from hinoki.runtime.security import https_redirect
from hinoki.settings import AppSettings, SecuritySettings


@pytest.mark.parametrize(
    "host", ["api.example.test\t", "api.example.test\x7f", "café.test"]
)
def test_https_redirect_refuses_non_header_safe_host_values(host: str) -> None:
    settings = AppSettings(
        allowed_hosts=("api.example.test",),
        security=SecuritySettings(redirect_https=True),
    )
    scope: dict[str, Any] = {
        "scheme": "http",
        "path": "/items",
        "raw_path": b"/items",
        "query_string": b"page=2",
    }
    assert https_redirect(scope, {"host": host}, settings) is None
