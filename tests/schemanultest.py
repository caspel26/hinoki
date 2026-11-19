"""A NUL character in text is a validation error: PostgreSQL cannot store it."""

from __future__ import annotations

import pytest

from hinoki import Hinoki, Schema, http
from hinoki.errors import ValidationError
from hinoki.testing import ASGIClient


class Note(Schema):
    title: str
    tags: dict[str, str]
    items: list[str]


GOOD = {"title": "a", "tags": {"k": "v"}, "items": ["x"]}
BAD = {
    "title": ({**GOOD, "title": "a\x00b"}, "nul"),
    "item": ({**GOOD, "items": ["ok", "\x00"]}, "nul"),
    "value": ({**GOOD, "tags": {"k": "v\x00"}}, "nul"),
    "key": ({**GOOD, "tags": {"a\x00": "v"}}, "key_nul"),
}


@pytest.mark.parametrize("case", BAD)
def test_nul_is_refused_on_the_python_lane(case: str) -> None:
    data, code = BAD[case]
    with pytest.raises(ValidationError) as caught:
        Note.validate(data)
    assert [d.code for d in caught.value.details] == [code]


@pytest.mark.parametrize("case", BAD)
@pytest.mark.asyncio
async def test_nul_is_a_422_on_the_request_body_lane(case: str) -> None:
    data, code = BAD[case]
    app = Hinoki(title="nul")

    @http.post("/notes")
    async def create(body: Note) -> dict[str, str]:
        return {"title": body.title}

    app.include(create)
    async with ASGIClient(app) as client:
        response = await client.post("/notes", json=data)
        ok = await client.post("/notes", json=GOOD)
    assert response.status == 422
    assert code in str(response.body)
    assert ok.status == 200
