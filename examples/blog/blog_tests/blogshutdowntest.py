"""The blog's password hasher is owned by Passwords and closed at shutdown."""

import pytest
from blog.apps.blog.accounts import Passwords
from blog.main import build_app
from blog.settings import BlogSettings

from hinoki.crypto import PasswordHasher
from hinoki.testing import ASGIClient


async def test_login_closes_its_hasher_at_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[PasswordHasher] = []
    real_close = PasswordHasher.close

    def record(self: PasswordHasher) -> None:
        closed.append(self)
        real_close(self)

    monkeypatch.setattr(PasswordHasher, "close", record)
    settings = BlogSettings.load(overrides={"jwt_secret": "s" * 40})

    app = build_app(settings)

    assert any(isinstance(module, Passwords) for module in app.modules)
    async with ASGIClient(app):
        assert closed == []
    assert len(closed) == 1


def test_without_login_there_is_no_hasher_to_close() -> None:
    app = build_app(BlogSettings.load(overrides={}))
    assert not any(isinstance(module, Passwords) for module in app.modules)
