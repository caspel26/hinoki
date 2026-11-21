"""Serving a request builds no typing aliases."""

from collections.abc import Callable
from typing import Any

import pytest

from hinoki import Hinoki, http
from tests.harness import Pool


@http.get("/plain")
async def plain() -> dict[str, int]:
    return {"a": 1}


async def test_a_request_evaluates_no_callable_alias(
    pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = Hinoki()
    app.include(plain)
    client = await pool.start(app)
    await client.request("GET", "/plain")
    built: list[Any] = []
    original = Callable.__class_getitem__  # type: ignore[attr-defined]

    def counting(args: Any) -> Any:
        built.append(args)
        return original(args)

    monkeypatch.setattr(Callable, "__class_getitem__", staticmethod(counting))
    reply = await client.request("GET", "/plain")
    assert reply.status == 200
    assert built == []
