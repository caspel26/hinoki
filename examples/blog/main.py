"""Application factory.

This example uses a factory because it conditionally installs accounts when the
JWT secret is configured and builds the optional admin from settings.

    BLOG_ADMIN_SECRET=... \
      hinoki dev blog.main:create_app

The factory builds declarations only; the pool opens in the worker's lifespan.
"""

from hinoki import Hinoki, Mount

from .accounts import build_accounts
from .adminsite import build_admin
from .api import Posts, health
from .rules import blog_rules
from .settings import BlogSettings


def create_app(
    database_url: str | None = None,
    admin_secret: str | None = None,
    jwt_secret: str | None = None,
) -> Hinoki:
    """Settings come from ``BLOG_*`` environment variables; an explicit argument
    (used by tests) overrides its variable."""
    given = {
        "database_url": database_url,
        "admin_secret": admin_secret,
        "jwt_secret": jwt_secret,
    }
    overrides: dict[str, object] = {k: v for k, v in given.items() if v is not None}
    database_override = overrides.pop("database_url", None)
    if isinstance(database_override, str):
        overrides["database"] = {"url": database_override}
    app_settings = BlogSettings.load(overrides=overrides)
    app = Hinoki(
        settings=app_settings,
        title="Blog",
        version="0.1.0",
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        rules=blog_rules,  # startup rules fail the app if a promise is broken
    )
    app.include(
        health,
        Posts,
        Mount(
            "/admin",
            build_admin(app_settings.admin_secret.reveal(), app_settings.admin_theme),
        ),
    )
    # password login is on only when a signing secret exists
    if app_settings.jwt_secret:
        actions, _hasher = build_accounts(app_settings.jwt_secret.reveal())
        app.include(*actions)
    return app
