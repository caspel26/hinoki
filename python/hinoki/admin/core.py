"""Admin: the site object that registers models and builds the API."""

from __future__ import annotations

import re
import types
from collections.abc import Awaitable, Callable
from datetime import UTC, tzinfo
from typing import Any, overload

from hinoki.access import Access
from hinoki.admin.components import Components, logo_fragment
from hinoki.admin.csrf import COOKIE as CSRF_COOKIE
from hinoki.admin.csrf import Csrf
from hinoki.admin.modeladmin import ModelAdmin
from hinoki.admin.parts import Event, Link
from hinoki.admin.site import SECURITY_HEADERS
from hinoki.admin.templates import Templates
from hinoki.admin.themes import Theme
from hinoki.admin.users.auth import (
    BASE_PERMISSION,
    EVERYTHING,
    AdminSessionAuth,
)
from hinoki.admin.users.auth import (
    COOKIE as SESSION_COOKIE,
)
from hinoki.admin.users.service import ACTIONS, AdminAccounts
from hinoki.admin.users.useradmin import ADMIN_TABLES, UserAdmin
from hinoki.admin.views import (
    ASSETS,
    AUTH_VIEWS,
    HASHING_VIEWS,
    PUBLIC_VIEWS,
    ROUTES,
    VIEWS,
)
from hinoki.api import API
from hinoki.auth import Caller
from hinoki.errors import RegistrationError
from hinoki.orm import Model
from hinoki.orm.naming import table_names_conflict
from hinoki.parsers import FormParser
from hinoki.renderers import HTMLRenderer
from hinoki.routing import Options, RouteDeclaration
from hinoki.settings import AdminSettings

SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")
RESERVED = {"static", "search", "login", "logout", "password"}


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


def no_secret_message(env_prefix: str = "") -> str:
    """The error for an admin with no secret; with the settings class prefix it
    names the real environment variable."""
    variable = (
        f"{env_prefix}ADMIN__SECRET"
        if env_prefix
        else '<prefix>ADMIN__SECRET, for example BLOG_ADMIN__SECRET with prefix="BLOG_"'
    )
    return (
        "admin: no secret to sign its forms with; set `admin.secret` in the app's "
        f"settings (environment variable {variable}, at least 32 bytes) "
        "or pass Admin(secret=...)"
    )


NO_SECRET_MESSAGE = no_secret_message()


class Admin:
    """The admin site: prefix, access policy, theme and registered models."""

    def __init__(  # noqa: PLR0913 - keyword-only site options
        self,
        *,
        access: Access | None = None,
        users: AdminAccounts | None = None,
        manage_users: bool = True,
        secret: str | bytes | None = None,
        prefix: str | None = None,
        theme: str | Theme | None = None,
        templates: str | None = None,
        site: str = "Admin",
        per_page: int = 20,
        show_count: bool = False,
        tagline: str = "",
        workspace: str = "Workspace",
        links: tuple[Link, ...] = (),
        logout_url: str = "",
        role: Callable[[Caller[Any]], str] | None = None,
        activity: Callable[[Caller[Any]], Awaitable[list[Event]]] | None = None,
        dashboard: tuple[str, ...] = (),
        secure_cookies: bool | None = None,
        logo: str = "",
    ) -> None:
        if prefix and (not prefix.startswith("/") or prefix.endswith("/")):
            raise ValueError("prefix must start with '/' and not end with '/'")
        if access is not None and users is not None:
            raise ValueError("pass access= (your own sign-in) or users=, not both")
        if access is None and users is None:
            users = AdminAccounts()  # the default: the admin's own users
        self.users = users
        self.access: Access = access or Access(
            auth=AdminSessionAuth(self), permissions=[BASE_PERMISSION]
        )
        self.secure_cookies = secure_cookies
        self._csrf = (
            Csrf(secret, self.cookie_name(CSRF_COOKIE)) if secret is not None else None
        )
        self._prefix = prefix
        self.per_page = per_page
        self.show_count = show_count
        self._served: str | None = None  # the prefix of the routes, once built
        self.mount = ""  # set when the admin is included under a ``Mount``
        self.site = site
        self.logo_mark = logo_fragment(logo)
        self.tagline = tagline
        self.workspace = workspace
        self.links = links
        self.logout_url = logout_url
        self.role = role or default_role
        self.activity = activity
        self.dashboard = dashboard
        self.timezone: tzinfo = UTC
        self._bound_timezone: tzinfo | None = None
        self._templates_directory = templates
        self._theme_given = theme is not None
        self.models: dict[str, ModelAdmin[Any]] = {}
        self.manage_users = manage_users
        self.use_theme(theme or "studio")

    def use_theme(self, theme: str | Theme) -> None:
        self.templates = Templates(self._templates_directory, theme)
        self.theme = self.templates.theme
        self.parts = Components(self.templates, self.timezone)
        self.digests = {
            "admin.css": digest(self.templates.stylesheet()),
            "admin.js": digest(self.templates.script()),
        }

    def cookie_name(self, name: str) -> str:
        """``__Secure-`` before the name when the admin is told its cookies are
        always secure (``secure_cookies=True``): a browser then refuses to set it
        over plain HTTP, so a sibling site cannot plant it."""
        return f"__Secure-{name}" if self.secure_cookies else name

    @property
    def session_cookie(self) -> str:
        return self.cookie_name(SESSION_COOKIE)

    @property
    def csrf(self) -> Csrf:
        if self._csrf is None:
            raise RegistrationError(NO_SECRET_MESSAGE)
        return self._csrf

    @property
    def accounts(self) -> AdminAccounts:
        if self.users is None:
            raise RegistrationError("admin: this site has no users= accounts")
        return self.users

    def bind_settings(
        self, settings: AdminSettings | None, env_prefix: str = ""
    ) -> None:
        """Use the app's ``admin`` settings: the secret (unless the admin was given
        its own), the theme (unless it was given one) and the session and
        password rules of its own users."""
        if self.users is not None and settings is not None:
            self.users.bind(settings)
        if settings is not None and settings.theme and not self._theme_given:
            self.use_theme(settings.theme)
        if self._csrf is not None:
            return
        if settings is None or settings.secret is None:
            raise RegistrationError(no_secret_message(env_prefix))
        self._csrf = Csrf(settings.secret.reveal(), self.cookie_name(CSRF_COOKIE))

    @property
    def prefix(self) -> str:
        """Where the admin lives under its ``Mount``: nothing when the admin is
        mounted and names no prefix, ``/admin`` when it is included as it is."""
        if self._served is not None:
            return self._served
        if self._prefix is not None:
            return self._prefix
        return "" if self.mount else "/admin"

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

    @overload
    def register[A: type[ModelAdmin[Any]]](
        self, target: A, /, *, slug: str | None = None
    ) -> A: ...
    @overload
    def register[M: Model](
        self,
        target: type[M],
        admin: type[ModelAdmin[M]] | None = None,
        *,
        slug: str | None = None,
    ) -> type[M]: ...
    def register(
        self,
        target: Any,
        admin: Any = None,
        *,
        slug: str | None = None,
    ) -> Any:
        """Add a model to the admin and return ``target``, so it also decorates.

            site = Admin(access=editors, secret=secret)

            @site.register
            class PostAdmin(ModelAdmin, model=Post): ...   # a customised admin

            site.register(User)                            # the default admin

        ``register(Post, PostAdmin, slug="posts")`` is the older two-argument form."""
        if isinstance(target, type) and issubclass(target, ModelAdmin):
            model = getattr(target, "model", None)
            if model is None:
                raise RegistrationError(
                    f"admin: {target.__name__} names no model; write "
                    f"class {target.__name__}(ModelAdmin, model=YourModel)"
                )
            self.attach(model, target, slug or target.slug or None)
        else:
            self.attach(target, admin, slug)
        return target

    def refuse_own_tables(self, model: type[Model], admin: object) -> None:
        """The admin's own account tables are managed by the Users section only: a
        plain model admin would show the password hash and let a grant holder edit
        who is a superuser."""
        plain = not (isinstance(admin, type) and issubclass(admin, UserAdmin))
        if self.users is not None and model in ADMIN_TABLES and plain:
            raise RegistrationError(
                f"admin: {model.__name__} holds the admin's own accounts; it is "
                "managed by the Users section, not registered as a model"
            )

    def admin[A: type[ModelAdmin[Any]]](
        self, model: type[Model], *, slug: str | None = None
    ) -> Callable[[A], A]:
        """Decorate a ``ModelAdmin`` class that does not name its model:

            @site.admin(Post)
            class PostAdmin(ModelAdmin):
                columns = ("title", "published")

        The class may also say ``model=Post`` itself; naming two different models
        is a ``RegistrationError``."""

        def decorate(cls: A) -> A:
            if not (isinstance(cls, type) and issubclass(cls, ModelAdmin)):
                raise RegistrationError(
                    "admin: @site.admin(Model) decorates a ModelAdmin class"
                )
            declared = getattr(cls, "model", None)
            if declared is not None and declared is not model:
                raise RegistrationError(
                    f"admin: {cls.__name__} is for {declared.__name__}, "
                    f"not {model.__name__}"
                )
            cls.model = model  # type: ignore[misc]
            self.attach(model, cls, slug or getattr(cls, "slug", None) or None)
            return cls

        return decorate

    def attach[M: Model](
        self,
        model: type[M],
        admin: type[ModelAdmin[M]] | None,
        slug: str | None,
    ) -> None:
        if model.__abstract__:
            raise RegistrationError(
                f"admin: {model.__name__} is abstract; register a model with a table"
            )
        model.check_table()
        self.refuse_own_tables(model, admin)
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
        declared = getattr(admin, "model", None)
        if declared is not None and declared is not model:
            raise RegistrationError(
                f"admin: {getattr(admin, '__name__', 'the admin class')} is for "
                f"{declared.__name__}, not {model.__name__}"
            )
        instance = (admin or ModelAdmin)()
        instance.model = model
        instance.site = self
        if instance.per_page is None:
            instance.per_page = self.per_page  # type: ignore[misc]
        if instance.show_count is None:
            instance.show_count = self.show_count  # type: ignore[misc]
        instance.validate_config(name)
        if self.users is not None:
            instance.can = granted(name, instance.can)  # type: ignore[method-assign, assignment]
        self.models[name] = instance

    def holds_no_connection(self, name: str) -> bool:
        """Routes that may hash a password take no route-wide transaction, so no
        connection waits on Argon2; their writes open their own, short one."""
        if name in HASHING_VIEWS:
            return True
        return self.users is not None and name in {"add_submit", "edit_submit"}

    def api(self) -> type[API]:
        """The ``API`` class to ``include`` in the application."""
        self.add_user_admin()
        self.check_dashboard()
        self._served = self.prefix  # links must match the routes built below
        namespace: dict[str, Any] = {
            "admin": self,
            "hide_from_docs": True,
            "response_headers": SECURITY_HEADERS,
        }
        for name, method, path, write in ROUTES:
            if name in AUTH_VIEWS and self.users is None:
                continue
            declare = RouteDeclaration(method, path, self.route_options(name, write))
            namespace[name] = declare(clone(VIEWS[name], name))
        namespace["bind_mount"] = staticmethod(self.bind_mount)
        return types.new_class(
            "AdminSite", (API,), {"path": self.prefix}, lambda ns: ns.update(namespace)
        )

    def add_user_admin(self) -> None:
        """Register the Users section last, after the application's own models."""
        if (
            self.users is not None
            and self.manage_users
            and not any(isinstance(each, UserAdmin) for each in self.models.values())
        ):
            self.register(UserAdmin)

    def check_dashboard(self) -> None:
        for slug in self.dashboard:
            if slug not in self.models:
                raise RegistrationError(
                    f"admin: dashboard names unknown model {slug!r}"
                )

    def route_options(self, name: str, write: bool) -> Options:
        """The options of one admin view: public views name no access policy, and
        the views that hash a password take the sign-in rate limit."""
        public = name in PUBLIC_VIEWS
        limited = name in HASHING_VIEWS and self.users is not None
        return Options(
            access=None if public else self.access,
            atomic=write and not self.holds_no_connection(name),
            parsers=[FormParser()] if write else None,
            renderers=None if public and name in ASSETS else [HTMLRenderer()],
            rate_limit=self.users.login_limit if limited and self.users else None,
        )

    @property
    def base(self) -> str:
        """The path the admin is reachable at: its own prefix under any ``Mount``."""
        return self.mount + self.prefix

    def bind_mount(self, mount: str) -> None:
        """Called by ``Hinoki.include`` with the prefix of the ``Mount`` around the
        admin, so generated links, assets and the CSRF cookie path match the routes."""
        self.mount = mount


def granted(
    slug: str, inner: Callable[..., bool]
) -> Callable[[Caller[Any], str, Any], bool]:
    """``ModelAdmin.can`` limited to what the user's grants allow, whatever the
    admin class's own ``can`` says: an override cannot widen a grant. An action
    that is not view/add/change/delete (a bulk action) needs ``change``."""

    def can(principal: Caller[Any], action: str, row: Any = None) -> bool:
        needed = action if action in ACTIONS else "change"
        held = principal.permissions
        return (EVERYTHING in held or f"{slug}:{needed}" in held) and inner(
            principal, action, row
        )

    return can


def digest(data: bytes) -> str:
    import hashlib  # noqa: PLC0415 - tiny helper, keeps the import list short

    return hashlib.sha256(data).hexdigest()[:10]


def default_role(principal: Caller[Any]) -> str:
    return ", ".join(sorted(principal.permissions)) or "Signed in"
