from __future__ import annotations

import pytest

from hinoki.http.request import media_type


def test_a_quoted_boundary_may_contain_a_comma() -> None:
    raw = 'multipart/form-data; boundary="a,b"'
    assert media_type(raw) == "multipart/form-data"


def test_repeated_content_types_are_still_refused() -> None:
    with pytest.raises(Exception, match="several Content-Type values"):
        media_type("application/json, text/plain")
