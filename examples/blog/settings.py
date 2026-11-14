"""The blog's settings: the framework's built-in groups plus its own fields.

BLOG_DATABASE_URL=postgres://...      required
BLOG_ADMIN_SECRET=...                 required, 32+ bytes
BLOG_JWT_SECRET=...                   optional: switches on /auth/login
BLOG_ADMIN_THEME=studio|console|paper|grove
BLOG_EXECUTION__SYNC_WORKERS=16       any built-in group: execution, lifecycle,
BLOG_LOGGING__FORMAT=json             logging, docs (see docs/SETTINGS-DESIGN.md)
"""

from typing import Literal

from hinoki import settings


class BlogSettings(settings.AppSettings, prefix="BLOG_"):
    database_url: settings.Secret
    admin_secret: settings.Secret
    jwt_secret: settings.Secret | None = None
    admin_theme: Literal["studio", "console", "paper", "grove"] = "studio"
