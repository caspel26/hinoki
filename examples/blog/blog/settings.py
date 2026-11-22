"""The blog's settings: the framework's built-in groups plus its own fields.

BLOG_DATABASE__URL=postgres://...     optional; defaults to local SQLite
BLOG_ADMIN__SECRET=...             required by the admin, 32+ bytes
BLOG_JWT_SECRET=...                   optional: switches on /auth/login
BLOG_METRICS__LISTEN=127.0.0.1:9100   optional, Kiso only: Prometheus on its own port
BLOG_EXECUTION__SYNC_WORKERS=16       any built-in group: execution, lifecycle,
BLOG_LOGGING__FORMAT=json             logging, docs (see docs/settings.md)

The admin theme is chosen where the site is built, in ``adminsite.py``.
"""

from hinoki import settings


class BlogSettings(settings.AppSettings, prefix="BLOG_"):
    database: settings.DatabaseSettings
    jwt_secret: settings.Secret | None = None
