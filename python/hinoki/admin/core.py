"""Admin: the site object that registers models and builds the API."""

from __future__ import annotations

import re
import types
from collections.abc import Awaitable, Callable
from datetime import UTC, tzinfo
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
from hinoki.auth import Caller
from hinoki.errors import RegistrationError
from hinoki.orm import Model
from hinoki.orm.naming import table_names_conflict
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
        role: Callable[[Caller[Any]], str] | None = None,
        activity: Callable[[Caller[Any]], Awaitable[list[Event]]] | None = None,
        dashboard: tuple[str, ...] = (),
        secure_cookies: bool | None = None,
    ) -> None:
        if not prefix.startswith("/") or prefix.endswith("/"):
            raise ValueError("prefix must start with '/' and not end with '/'")
        self.access = access
        self.csrf = Csrf(secret)
        self.prefix = prefix
        self.mount = ""  # set when the admin is included under a ``Mount``
        self.site = site
        self.tagline = tagline
        self.workspace = workspace
        self.links = links
        self.logout_url = logout_url
        self.role = role or default_role
        self.activity = activity
        self.dashboard = dashboard
        self.secure_cookies = secure_cookies
        self.timezone: tzinfo = UTC
        self._bound_timezone: tzinfo | None = None
        self.templates = Templates(templates, theme)
        self.theme = self.templates.theme
        self.parts = Components(self.templates)
        self.models: dict[str, ModelAdmin[Any]] = {}
        self.digests = {
            "admin.css": digest(self.templates.stylesheet()),
            "admin.js": digest(self.templates.script()),
        }

    def bind_timezone(self, timezone: tzinfo) -> None:
        """Use the including app's timezone; reject conflicting app bindings."""
        if self._bound_timezone is not None and str(self._bound_timezone) != str(
            timezone
        ):
            raise RegistrationError(
                "an Admin instance cannot be shared by apps with different timezones"
            )
        self._bound_timezone = timezone
        self.timezone = timezone
        self.parts.timezone = timezone

    def register[M: Model](
        self,
        model: type[M],
        admin: type[ModelAdmin[M]] | None = None,
        *,
        slug: str | None = None,
    ) -> None:
        if model.__abstract__:
            raise RegistrationError(
                f"admin: {model.__name__} is abstract; register a model with a table"
            )
        model.check_table()
        for existing in self.models.values():
            if existing.model is not model and table_names_conflict(
                existing.model, model
            ):
                raise RegistrationError(
                    f"admin: models {existing.model.__module__}."
                    f"{existing.model.__qualname__} and {model.__module__}."
                    f"{model.__qualname__} have conflicting table names "
                    f"{existing.model.__table__!r} and {model.__table__!r}"
                )
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
        namespace["bind_mount"] = staticmethod(self.bind_mount)
        return types.new_class(
            "AdminSite", (API,), {"path": self.prefix}, lambda ns: ns.update(namespace)
        )

    @property
    def base(self) -> str:
        """The path the admin is reachable at: its own prefix under any ``Mount``."""
        return self.mount + self.prefix

    def bind_mount(self, mount: str) -> None:
        """Called by ``Hinoki.include`` with the prefix of the ``Mount`` around the
        admin, so generated links, assets and the CSRF cookie path match the routes."""
        self.mount = mount


def digest(data: bytes) -> str:
    import hashlib  # noqa: PLC0415 - tiny helper, keeps the import list short

    return hashlib.sha256(data).hexdigest()[:10]


def default_role(principal: Caller[Any]) -> str:
    return ", ".join(sorted(principal.permissions)) or "Signed in"
