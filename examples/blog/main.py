"""The Blog application.

From ``examples/``::

    BLOG_ADMIN__SECRET=... uv run hinoki dev blog.main:app

Settings come from ``BLOG_*`` variables; the admin needs ``BLOG_ADMIN__SECRET``.
Password login is included only when ``BLOG_JWT_SECRET`` is set. Importing this
module builds declarations only; the pool opens in the worker's lifespan.
"""

from collections.abc import Callable
from typing import Any

from hinoki import Hinoki, ModuleContext

from .apps.blog.accounts import build_accounts
from .routes import routes
from .rules import blog_rules
from .settings import BlogSettings


def build_app(settings: BlogSettings) -> Hinoki:
    """The blog; with a JWT secret it also owns a hasher, closed at shutdown."""
    actions: list[Callable[..., Any]] = []
    on_shutdown = []
    if settings.jwt_secret:
        accounts, hasher = build_accounts(settings.jwt_secret.reveal())
        actions.extend(accounts)

        def close_hasher(_ctx: ModuleContext) -> None:
            hasher.close()

        on_shutdown.append(close_hasher)
    return Hinoki(
        settings=settings,
        title="Blog",
        version="0.1.0",
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        include=[*routes, *actions],
        rules=blog_rules,  # startup rules fail the app if a promise is broken
        on_shutdown=on_shutdown,
    )


app = build_app(BlogSettings.load())  # BLOG_* variables, read once at import
