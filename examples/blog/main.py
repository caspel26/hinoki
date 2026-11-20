"""The Blog application.

From ``examples/``::

    BLOG_ADMIN__SECRET=... uv run hinoki dev blog.main:app

Settings come from ``BLOG_*`` variables; the admin needs ``BLOG_ADMIN__SECRET``.
Password login is included only when ``BLOG_JWT_SECRET`` is set. Importing this
module builds declarations only; the pool opens in the worker's lifespan.
"""

from hinoki import Hinoki

from .apps.blog.accounts import build_accounts
from .routes import routes
from .rules import blog_rules
from .settings import BlogSettings

settings = BlogSettings.load()  # BLOG_* variables, read once at import
app = Hinoki(
    settings=settings,
    title="Blog",
    version="0.1.0",
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
    include=routes,
    rules=blog_rules,  # startup rules fail the app if a promise is broken
)
if settings.jwt_secret:
    actions, _hasher = build_accounts(settings.jwt_secret.reveal())
    app.include(*actions)
