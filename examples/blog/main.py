"""Application factory.

    BLOG_DATABASE_URL=postgres://... \
      granian --interface asgi --factory examples.blog.main:create_app

The factory builds declarations only; the pool opens in the worker's lifespan.
"""

import os

from examples.blog.adminsite import build_admin
from examples.blog.api import Posts, health
from examples.blog.rules import blog_rules
from hinoki import Conflict, HinokiAPI
from hinoki.db import DatabaseModule, UniqueViolation


def unique_to_conflict(_error: UniqueViolation) -> Conflict:
    return Conflict("A unique value is already in use.", code="unique_conflict")


def create_app(
    database_url: str | None = None, admin_secret: str | None = None
) -> HinokiAPI:
    url = database_url or os.environ["BLOG_DATABASE_URL"]
    secret = admin_secret or os.environ["BLOG_ADMIN_SECRET"]  # at least 32 bytes
    app = HinokiAPI(
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
        build_admin(secret, os.environ.get("BLOG_ADMIN_THEME", "studio")).api(),
    )
    app.install(DatabaseModule(url, max_size=5))
    app.errors.handle(UniqueViolation)(unique_to_conflict)
    return app
