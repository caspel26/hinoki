"""The blog's password hasher is closed when the worker shuts down."""

import asyncio
from typing import Any

import pytest
from blog.main import build_app
from blog.settings import BlogSettings

from hinoki.crypto import PasswordHasher


def test_login_closes_its_hasher_at_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[PasswordHasher] = []

    def record(self: PasswordHasher) -> None:
        closed.append(self)

    monkeypatch.setattr(PasswordHasher, "close", record)
    settings = BlogSettings.load(overrides={"jwt_secret": "s" * 40})

    app = build_app(settings)

    assert len(app.on_shutdown) == 1
    asyncio.run(_run(app.on_shutdown[0]))
    assert len(closed) == 1


def test_without_login_there_is_no_hasher_to_close() -> None:
    assert build_app(BlogSettings.load(overrides={})).on_shutdown == ()


async def _run(hook: Any) -> None:
    result = hook(None)
    if result is not None:
        await result
