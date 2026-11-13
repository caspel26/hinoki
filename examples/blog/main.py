"""Application factory.

    BLOG_DATABASE_URL=postgres://... \
      python -m hinoki dev examples.blog.main:create_app

The factory builds declarations only; the pool opens in the worker's lifespan.
"""

from examples.blog.accounts import build_accounts
from examples.blog.adminsite import build_admin
from examples.blog.api import Posts, health
from examples.blog.rules import blog_rules
from examples.blog.settings import BlogSettings
from hinoki import Conflict, HinokiAPI
from hinoki.db import DatabaseModule, UniqueViolation


def unique_to_conflict(_error: UniqueViolation) -> Conflict:
    return Conflict("A unique value is already in use.", code="unique_conflict")


def create_app(
    database_url: str | None = None,
    admin_secret: str | None = None,
    jwt_secret: str | None = None,
) -> HinokiAPI:
    """Settings come from ``BLOG_*`` environment variables; an explicit argument
    (used by tests) overrides its variable."""
    given = {
        "database_url": database_url,
        "admin_secret": admin_secret,
        "jwt_secret": jwt_secret,
    }
    settings = BlogSettings.load(
        overrides={k: v for k, v in given.items() if v is not None}
    )
    app = HinokiAPI(
        settings=settings,
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
        build_admin(settings.admin_secret.reveal(), settings.admin_theme).api(),
    )
    if settings.jwt_secret:  # password login is on only when a signing secret exists
        actions, _hasher = build_accounts(settings.jwt_secret.reveal())
        app.include(*actions)
    app.install(DatabaseModule(settings.database_url.reveal(), max_size=5))
    app.errors.handle(UniqueViolation)(unique_to_conflict)
    return app
