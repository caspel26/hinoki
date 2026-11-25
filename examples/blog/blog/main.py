"""The Blog application.

From ``examples/blog``::

    BLOG_ADMIN__SECRET=... uv run hinoki dev

Settings come from ``BLOG_*`` variables; the admin needs ``BLOG_ADMIN__SECRET``.
Password login is included only when ``BLOG_JWT_SECRET`` is set. Routes are listed
here because the accounts are optional; the events of the ``blog`` app come from
``hinoki.toml``. ``POST /mcp`` exposes one read-only tool with the caller's own
token. Importing this module builds declarations only; the pool opens in the
worker's lifespan.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from hinoki import Hinoki, Module
from hinoki.ai.mcpapp import expose_as_mcp

from .apps.blog.accounts import build_accounts
from .apps.blog.security import editor
from .routes import routes
from .rules import blog_rules
from .settings import BlogSettings

MCP_TOOLS = ["blog.apps.blog.api.Posts.drafts"]
# The owner's waivers, found whatever directory the app is started from.
WAIVERS = Path(__file__).parents[1] / "hinoki-rules.toml"


def build_app(settings: BlogSettings) -> Hinoki:
    """The blog; with a JWT secret it also mounts the worker-owned Passwords module."""
    actions: list[Callable[..., Any]] = []
    modules: list[Module] = []
    if settings.jwt_secret:
        accounts, passwords = build_accounts(settings.jwt_secret)
        actions.extend(accounts)
        modules.append(passwords)
    app = Hinoki(
        settings=settings,
        title="Blog",
        version="0.1.0",
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        include=[*routes, *actions],
        modules=modules,
        rules=blog_rules,  # startup rules fail the app if a promise is broken
        waivers=WAIVERS,
    )
    app.include(*expose_as_mcp(app, tools=MCP_TOOLS, access=editor))
    return app


app = build_app(BlogSettings.load())  # BLOG_* variables, read once at import
