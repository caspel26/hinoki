"""Admin and ModelAdmin: the pages, as ordinary Hinoki routes.

``Admin.api()`` returns an ``API`` class whose actions are real declarations
protected by the admin's ``Access`` policy, so they appear in OpenAPI, rules and
the development MCP. See ``docs/ADMIN-DESIGN.md`` for what the first slice does
and does not do.
"""

from __future__ import annotations

import dataclasses
import datetime
import re
import types
import typing
from collections.abc import Callable
from typing import Any, ClassVar
from urllib.parse import urlencode

from hinoki.access import Access
from hinoki.admin import csrf
from hinoki.admin.forms import (
    FormField,
    collect,
    fields_for,
    render_field,
    show,
)
from hinoki.admin.templates import Raw, Templates, escape
from hinoki.api import API
from hinoki.auth import Principal
from hinoki.errors import RegistrationError
from hinoki.orm import Model, QuerySet
from hinoki.pagination import PageParams
from hinoki.parsers import FormParser
from hinoki.renderers import HTMLRenderer
from hinoki.reply import Reply
from hinoki.request import Request
from hinoki.routing import Options, RouteDeclaration
from hinoki.runtime.binding import to_int
from hinoki.runtime.responses import Response  # noqa: F401 - documents the layer

SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")
RESERVED = {"static"}
SECURITY_HEADERS = (
    ("content-security-policy", "default-src 'self'; frame-ancestors 'none'"),
    ("x-content-type-options", "nosniff"),
    ("cache-control", "no-store"),
    ("referrer-policy", "same-origin"),
)
NOTICES = {"created": "Created.", "saved": "Saved.", "deleted": "Deleted."}
CELL_LIMIT = 80
NO_HTML = Raw("")


class ModelAdmin[M: Model]:
    """How one model appears in the admin. Subclass and override what you need.

    ``queryset`` scopes every list and lookup; ``can`` gates ``view``, ``add``,
    ``change`` and ``delete``; ``save`` and ``remove`` are the single places that
    write, so an application can run a domain command there instead of writing
    around its rules.
    """

    model: type[M]
    columns: ClassVar[tuple[str, ...]] = ()
    search: ClassVar[tuple[str, ...]] = ()
    exclude: ClassVar[tuple[str, ...]] = ()
    per_page: ClassVar[int] = 20

    def queryset(self, principal: Principal[Any]) -> QuerySet[M]:  # noqa: ARG002 - hook
        return self.model.all()

    def can(self, principal: Principal[Any], action: str, row: M | None = None) -> bool:  # noqa: ARG002 - overridable hook
        return True

    def label(self, row: M) -> str:
        return f"{type(row).__name__} {getattr(row, self.model.__primary_key__)}"

    async def save(
        self, values: dict[str, Any], principal: Principal[Any], row: M | None = None
    ) -> M:
        if row is None:
            return await self.model.acreate(**values)
        key = self.model.__primary_key__
        found = (
            await self.queryset(principal)
            .filter(**{key: getattr(row, key)})
            .aupdate(values)
        )
        if not found:
            raise self.model.NotFound(f"{self.model.__name__} not found")
        return found[0]

    async def remove(self, row: M, principal: Principal[Any]) -> None:
        key = self.model.__primary_key__
        gone = (
            await self.queryset(principal).filter(**{key: getattr(row, key)}).adelete()
        )
        if not gone:
            raise self.model.NotFound(f"{self.model.__name__} not found")

    def shown_columns(self) -> tuple[str, ...]:
        return self.columns or tuple(self.model.__columns__[:6])


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
    """The admin site: a prefix, an access policy, templates and registered models."""

    def __init__(
        self,
        *,
        access: Access,
        prefix: str = "/admin",
        templates: str | None = None,
        site: str = "Admin",
    ) -> None:
        if not prefix.startswith("/") or prefix.endswith("/"):
            raise ValueError("prefix must start with '/' and not end with '/'")
        self.access = access
        self.prefix = prefix
        self.site = site
        self.templates = Templates(templates)
        self.models: dict[str, ModelAdmin[Any]] = {}

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
        self.models[name] = instance

    def api(self) -> type[API]:
        """The ``API`` class to ``include`` in the application."""
        namespace: dict[str, Any] = {"admin": self, "hide_from_docs": True}
        for name, method, path, write in ROUTES:
            options = Options(
                access=None if name == "stylesheet" else self.access,
                transaction=write,
                parsers=[FormParser()] if write else None,
                renderers=None if name == "stylesheet" else [HTMLRenderer()],
            )
            declare = RouteDeclaration(method, path, options)
            namespace[name] = declare(clone(VIEWS[name], name))
        return types.new_class(
            "AdminSite",
            (API,),
            {"path": self.prefix},
            lambda ns: ns.update(namespace),
        )


class Site:
    """One request: who is asking, which token to embed, how to answer."""

    def __init__(
        self, admin: Admin, request: Request, principal: Principal[Any]
    ) -> None:
        self.admin = admin
        self.request = request
        self.principal = principal
        self.token, self.fresh = csrf.issue(request)

    def url(self, *parts: str, **query: str) -> str:
        path = "/".join([self.admin.prefix, *parts])
        suffix = "?" + urlencode(query) if query else ""
        return path + suffix

    def nav(self, active: str) -> Raw:
        items = []
        for slug, entry in self.admin.models.items():
            if not entry.can(self.principal, "view"):
                continue
            current = ' aria-current="page"' if slug == active else ""
            items.append(
                f'<li><a href="{escape(self.url(slug))}"{current}>'
                f"{escape(entry.model.__name__)}</a></li>"
            )
        return Raw("<ul>" + "".join(items) + "</ul>")

    def reply(
        self,
        title: str,
        content: Raw,
        *,
        status: int = 200,
        active: str = "",
        notice: Raw = NO_HTML,
    ) -> Reply:
        templates = self.admin.templates
        html = templates.render(
            "layout",
            title=title,
            site=self.admin.site,
            stylesheet=self.url("static", "admin.css"),
            home=self.url(),
            user=self.principal.subject,
            nav=self.nav(active),
            notice=notice,
            content=content,
        )
        headers = list(SECURITY_HEADERS)
        if self.fresh:
            headers.append(
                csrf.cookie_header(self.request, self.token, self.admin.prefix)
            )
        return Reply(status, html, headers, "text/html; charset=utf-8")

    def message(self, kind: str, text: str) -> Raw:
        role = "alert" if kind == "error" else "status"
        return self.admin.templates.render("notice", kind=kind, role=role, message=text)

    def problem(self, status: int, text: str) -> Reply:
        return self.reply(
            f"{status}", Raw(""), status=status, notice=self.message("error", text)
        )

    def entry(self, slug: str) -> ModelAdmin[Any] | None:
        found = self.admin.models.get(slug)
        return found if found and found.can(self.principal, "view") else None


def parse_key(entry: ModelAdmin[Any], raw: str) -> Any:
    """The primary key from its URL text, or ``None`` when it cannot be one."""
    kind = typing.get_type_hints(entry.model)[entry.model.__primary_key__]
    try:
        if kind is int:
            return to_int(raw)
        if kind is str:
            return raw
        return kind(raw)
    except (ValueError, TypeError):
        return None


async def find_row(site: Site, entry: ModelAdmin[Any], raw: str) -> Any:
    key = parse_key(entry, raw)
    if key is None:
        return None
    query = entry.queryset(site.principal).filter(**{entry.model.__primary_key__: key})
    return await query.afirst()


def cell(value: Any) -> str:
    if isinstance(value, bool):
        text = "yes" if value else "no"
    elif isinstance(value, datetime.datetime):
        text = value.astimezone(datetime.UTC).strftime("%Y-%m-%d %H:%M")
    else:
        text = show(value)
    if text == "":
        return "—"
    return text if len(text) <= CELL_LIMIT else text[: CELL_LIMIT - 1] + "…"


def table_rows(site: Site, slug: str, entry: ModelAdmin[Any], rows: list[Any]) -> Raw:
    columns = entry.shown_columns()
    key = entry.model.__primary_key__
    out = []
    for row in rows:
        cells = []
        for index, name in enumerate(columns):
            text = escape(cell(getattr(row, name)))
            if index == 0 and entry.can(site.principal, "change", row):
                link = escape(site.url(slug, str(getattr(row, key))))
                text = Raw(f'<a href="{link}">{text}</a>')
            cells.append(f"<td>{text}</td>")
        out.append("<tr>" + "".join(cells) + "</tr>")
    if not out:
        span = len(columns)
        return Raw(f'<tr><td class="empty" colspan="{span}">No rows.</td></tr>')
    return Raw("\n".join(out))


def pager(
    site: Site, slug: str, query: str, number: int, pages: int, total: int
) -> Raw:
    parts = [f"{total} row(s)"]
    if pages > 1:
        parts.append(f"page {number} of {pages}")
    for label, target, show_it in (
        ("Previous", number - 1, number > 1),
        ("Next", number + 1, number < pages),
    ):
        if show_it:
            params = {"page": str(target), **({"q": query} if query else {})}
            parts.append(f'<a href="{escape(site.url(slug, **params))}">{label}</a>')
    return Raw(" ".join(f"<span>{p}</span>" if "<a" not in p else p for p in parts))


async def stylesheet(self: API, request: Request) -> Reply:  # noqa: ARG001
    admin: Admin = self.admin  # type: ignore[attr-defined]
    return Reply(
        200,
        admin.templates.stylesheet(),
        [
            ("cache-control", "public, max-age=300"),
            ("x-content-type-options", "nosniff"),
        ],
        "text/css; charset=utf-8",
    )


async def index(self: API, request: Request, principal: Principal[Any]) -> Reply:
    site = Site(self.admin, request, principal)  # type: ignore[attr-defined]
    items = []
    for slug, entry in site.admin.models.items():
        if not entry.can(principal, "view"):
            continue
        count = await entry.queryset(principal).acount()
        items.append(
            f'<li><a href="{escape(site.url(slug))}">{escape(entry.model.__name__)}</a>'
            f'<span class="count">{count} row(s)</span></li>'
        )
    content = site.admin.templates.render("index", items=Raw("\n".join(items)))
    return site.reply("Dashboard", content)


async def listing(
    self: API,
    request: Request,
    principal: Principal[Any],
    model: str,
    q: str = "",
    page: int = 1,
    done: str = "",
) -> Reply:
    site = Site(self.admin, request, principal)  # type: ignore[attr-defined]
    entry = site.entry(model)
    if entry is None:
        return site.problem(404, "No such model.")
    query_set = entry.queryset(principal)
    if q and entry.search:
        query_set = query_set.filter(**{f"{entry.search[0]}__contains": q})
    found = await query_set.apaginate(PageParams(max(page, 1), entry.per_page))
    columns = entry.shown_columns()
    add = ""
    if entry.can(principal, "add"):
        link = escape(site.url(model, "add"))
        add = f'<a class="btn btn-primary" href="{link}">Add</a>'
    content = site.admin.templates.render(
        "table",
        action=site.url(model),
        query=q,
        caption=f"{entry.model.__name__} list",
        add=Raw(add),
        head=Raw("".join(f'<th scope="col">{escape(c)}</th>' for c in columns)),
        rows=table_rows(site, model, entry, found.items),
        pager=pager(site, model, q, found.page, found.pages, found.total),
    )
    notice = site.message("ok", NOTICES[done]) if done in NOTICES else Raw("")
    return site.reply(entry.model.__name__, content, active=model, notice=notice)


@dataclasses.dataclass(slots=True)
class FormView:
    """What a form page shows: the fields with their values and problems."""

    slug: str
    fields: list[FormField]
    action: str
    title: str
    values: dict[str, Any] = dataclasses.field(default_factory=dict)
    errors: dict[str, str] = dataclasses.field(default_factory=dict)
    status: int = 200


def form_page(site: Site, view: FormView) -> Reply:
    rendered = "".join(
        render_field(
            site.admin.templates, f, view.values.get(f.name), view.errors.get(f.name)
        )
        for f in view.fields
    )
    summary = NO_HTML
    if view.errors:
        summary = site.message("error", "Please correct the highlighted fields.")
    content = site.admin.templates.render(
        "form",
        action=view.action,
        csrf=site.token,
        summary=summary,
        fields=Raw(rendered),
        submit="Save",
        cancel=site.url(view.slug),
    )
    return site.reply(view.title, content, status=view.status, active=view.slug)


def skipped(entry: ModelAdmin[Any]) -> frozenset[str]:
    return frozenset(entry.exclude)


async def add_form(
    self: API, request: Request, principal: Principal[Any], model: str
) -> Reply:
    site = Site(self.admin, request, principal)  # type: ignore[attr-defined]
    entry = site.entry(model)
    if entry is None or not entry.can(principal, "add"):
        return site.problem(404, "No such model.")
    fields = fields_for(entry.model, skipped(entry), creating=True)
    title = f"Add {entry.model.__name__}"
    return form_page(site, FormView(model, fields, site.url(model, "add"), title))


async def add_submit(
    self: API, request: Request, principal: Principal[Any], model: str
) -> Reply:
    site = Site(self.admin, request, principal)  # type: ignore[attr-defined]
    entry = site.entry(model)
    if entry is None or not entry.can(principal, "add"):
        return site.problem(404, "No such model.")
    if not csrf.valid(request):
        return site.problem(
            403, "The form expired or is invalid. Reload and try again."
        )
    fields = fields_for(entry.model, skipped(entry), creating=True)
    values, errors = collect(fields, request.POST)
    if errors:
        title = f"Add {entry.model.__name__}"
        view = FormView(
            model,
            fields,
            site.url(model, "add"),
            title,
            dict(request.POST),
            errors,
            422,
        )
        return form_page(site, view)
    await entry.save(values, principal)
    return Reply(303, headers=[("location", site.url(model, done="created"))])


async def edit_form(
    self: API, request: Request, principal: Principal[Any], model: str, key: str
) -> Reply:
    site = Site(self.admin, request, principal)  # type: ignore[attr-defined]
    entry = site.entry(model)
    row = await find_row(site, entry, key) if entry else None
    if entry is None or row is None or not entry.can(principal, "change", row):
        return site.problem(404, "Not found.")
    fields = fields_for(entry.model, skipped(entry), creating=False)
    values = {f.name: getattr(row, f.name) for f in fields}
    return form_page(
        site, FormView(model, fields, site.url(model, key), entry.label(row), values)
    )


async def edit_submit(
    self: API, request: Request, principal: Principal[Any], model: str, key: str
) -> Reply:
    site = Site(self.admin, request, principal)  # type: ignore[attr-defined]
    entry = site.entry(model)
    row = await find_row(site, entry, key) if entry else None
    if entry is None or row is None or not entry.can(principal, "change", row):
        return site.problem(404, "Not found.")
    if not csrf.valid(request):
        return site.problem(
            403, "The form expired or is invalid. Reload and try again."
        )
    fields = fields_for(entry.model, skipped(entry), creating=False)
    values, errors = collect(fields, request.POST)
    if errors:
        view = FormView(
            model,
            fields,
            site.url(model, key),
            entry.label(row),
            dict(request.POST),
            errors,
            422,
        )
        return form_page(site, view)
    await entry.save(values, principal, row)
    return Reply(303, headers=[("location", site.url(model, done="saved"))])


async def delete_form(
    self: API, request: Request, principal: Principal[Any], model: str, key: str
) -> Reply:
    site = Site(self.admin, request, principal)  # type: ignore[attr-defined]
    entry = site.entry(model)
    row = await find_row(site, entry, key) if entry else None
    if entry is None or row is None or not entry.can(principal, "delete", row):
        return site.problem(404, "Not found.")
    content = site.admin.templates.render(
        "confirm",
        action=site.url(model, key, "delete"),
        csrf=site.token,
        message=f"Delete {entry.label(row)}? This cannot be undone.",
        cancel=site.url(model, key),
    )
    return site.reply("Delete", content, active=model)


async def delete_submit(
    self: API, request: Request, principal: Principal[Any], model: str, key: str
) -> Reply:
    site = Site(self.admin, request, principal)  # type: ignore[attr-defined]
    entry = site.entry(model)
    row = await find_row(site, entry, key) if entry else None
    if entry is None or row is None or not entry.can(principal, "delete", row):
        return site.problem(404, "Not found.")
    if not csrf.valid(request):
        return site.problem(
            403, "The form expired or is invalid. Reload and try again."
        )
    await entry.remove(row, principal)
    return Reply(303, headers=[("location", site.url(model, done="deleted"))])


VIEWS: dict[str, Callable[..., Any]] = {
    "stylesheet": stylesheet,
    "index": index,
    "listing": listing,
    "add_form": add_form,
    "add_submit": add_submit,
    "edit_form": edit_form,
    "edit_submit": edit_submit,
    "delete_form": delete_form,
    "delete_submit": delete_submit,
}
ROUTES: list[tuple[str, str, str, bool]] = [
    ("stylesheet", "GET", "/static/admin.css", False),
    ("index", "GET", "/", False),
    ("listing", "GET", "/{model}", False),
    ("add_form", "GET", "/{model}/add", False),
    ("add_submit", "POST", "/{model}/add", True),
    ("edit_form", "GET", "/{model}/{key}", False),
    ("edit_submit", "POST", "/{model}/{key}", True),
    ("delete_form", "GET", "/{model}/{key}/delete", False),
    ("delete_submit", "POST", "/{model}/{key}/delete", True),
]
