"""Admin: the site object that registers models and builds the API."""

from __future__ import annotations

import re
import types
from collections.abc import Awaitable, Callable
from typing import Any

from hinoki.access import Access
from hinoki.admin.components import Components
from hinoki.admin.csrf import Csrf
from hinoki.admin.modeladmin import ModelAdmin
from hinoki.admin.parts import Event, Link
from hinoki.admin.site import SECURITY_HEADERS
from hinoki.admin.templates import Templates
from hinoki.admin.themes import Theme
from hinoki.admin.views import ROUTES, VIEWS
from hinoki.api import API
from hinoki.auth import Principal
from hinoki.errors import RegistrationError
from hinoki.orm import Model
from hinoki.parsers import FormParser
from hinoki.renderers import HTMLRenderer
from hinoki.routing import Options, RouteDeclaration

SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")
RESERVED = {"static", "search"}


def clone(function: Callable[..., Any], name: str) -> Callable[..., Any]:
    """A copy of ``function`` so each admin declares its own routes."""
    copy = types.FunctionType(
        function.__code__,
        function.__globals__,
        name,
        function.__defaults__,
        function.__closure__,
    )
    copy.__kwdefaults__ = function.__kwdefaults__
    copy.__annotations__ = dict(function.__annotations__)
    copy.__qualname__ = name
    return copy


class Admin:
    """The admin site: prefix, access policy, theme and registered models."""

    def __init__(  # noqa: PLR0913 - keyword-only site options
        self,
        *,
        access: Access,
        secret: str | bytes,
        prefix: str = "/admin",
        theme: str | Theme = "studio",
        templates: str | None = None,
        site: str = "Admin",
        tagline: str = "",
        workspace: str = "Workspace",
        links: tuple[Link, ...] = (),
        logout_url: str = "",
        role: Callable[[Principal[Any]], str] | None = None,
        activity: Callable[[Principal[Any]], Awaitable[list[Event]]] | None = None,
        dashboard: tuple[str, ...] = (),
        secure_cookies: bool | None = None,
    ) -> None:
        if not prefix.startswith("/") or prefix.endswith("/"):
            raise ValueError("prefix must start with '/' and not end with '/'")
        self.access = access
        self.csrf = Csrf(secret)
        self.prefix = prefix
        self.site = site
        self.tagline = tagline
        self.workspace = workspace
        self.links = links
        self.logout_url = logout_url
        self.role = role or default_role
        self.activity = activity
        self.dashboard = dashboard
        self.secure_cookies = secure_cookies
        self.templates = Templates(templates, theme)
        self.theme = self.templates.theme
        self.parts = Components(self.templates)
        self.models: dict[str, ModelAdmin[Any]] = {}
        self.digests = {
            "admin.css": digest(self.templates.stylesheet()),
            "admin.js": digest(self.templates.script()),
        }

    def register[M: Model](
        self,
        model: type[M],
        admin: type[ModelAdmin[M]] | None = None,
        *,
        slug: str | None = None,
    ) -> None:
        name = slug or model.__table__.replace("_", "-").lower()
        if not SLUG.fullmatch(name) or name in RESERVED or name in self.models:
            raise RegistrationError(f"admin: invalid or duplicate slug {name!r}")
        instance = (admin or ModelAdmin)()
        instance.model = model
        instance.validate_config(name)
        self.models[name] = instance

    def api(self) -> type[API]:
        """The ``API`` class to ``include`` in the application."""
        for slug in self.dashboard:
            if slug not in self.models:
                raise RegistrationError(
                    f"admin: dashboard names unknown model {slug!r}"
                )
        namespace: dict[str, Any] = {
            "admin": self,
            "hide_from_docs": True,
            "response_headers": SECURITY_HEADERS,
        }
        for name, method, path, write in ROUTES:
            public = name in {"stylesheet", "script"}
            options = Options(
                access=None if public else self.access,
                transaction=write,
                parsers=[FormParser()] if write else None,
                renderers=None if public else [HTMLRenderer()],
            )
            declare = RouteDeclaration(method, path, options)
            namespace[name] = declare(clone(VIEWS[name], name))
        return types.new_class(
            "AdminSite", (API,), {"path": self.prefix}, lambda ns: ns.update(namespace)
        )


def digest(data: bytes) -> str:
    import hashlib  # noqa: PLC0415 - tiny helper, keeps the import list short

    return hashlib.sha256(data).hexdigest()[:10]


def default_role(principal: Principal[Any]) -> str:
    return ", ".join(sorted(principal.scopes)) or "Signed in"
