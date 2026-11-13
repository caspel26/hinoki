"""Admin and ModelAdmin: the pages, as ordinary Hinoki routes.

``Admin.api()`` returns an ``API`` class whose actions are real declarations
protected by the admin's ``Access`` policy, so they appear in rules and the
development MCP (they are left out of OpenAPI: they are HTML pages). Layout,
colours and the edit form's placement come from a theme (``themes.py``); the
components are the base templates in ``ui/components``. See
``docs/ADMIN-DESIGN.md`` for what is and is not provided.
"""

from __future__ import annotations

import dataclasses
import re
import types
import typing
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, ClassVar
from urllib.parse import quote, urlencode

from hinoki.access import Access
from hinoki.admin.components import Components, icon_name, initials, page_window
from hinoki.admin.csrf import Csrf, cookie_header
from hinoki.admin.forms import (
    FormField,
    collect,
    fields_for,
    input_text,
    render_field,
)
from hinoki.admin.parts import Avatar, Badge, Event, Fieldset, Link, Stat, Tab
from hinoki.admin.templates import Raw, Templates, escape
from hinoki.admin.themes import Theme
from hinoki.api import API
from hinoki.auth import Principal
from hinoki.db import (
    CheckViolation,
    DatabaseError,
    ForeignKeyViolation,
    NotNullViolation,
    UniqueViolation,
)
from hinoki.errors import RegistrationError
from hinoki.orm import Model, QuerySet, current_database
from hinoki.parsers import FormParser
from hinoki.renderers import HTMLRenderer
from hinoki.reply import Reply
from hinoki.request import Request
from hinoki.routing import Options, RouteDeclaration
from hinoki.runtime.binding import to_int

SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")
RESERVED = {"static", "search"}
CSP = "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
SECURITY_HEADERS = (
    ("content-security-policy", CSP),
    ("x-content-type-options", "nosniff"),
    ("cache-control", "no-store"),
    ("referrer-policy", "same-origin"),
)
NOTICES = {"created": "Created.", "saved": "Saved.", "deleted": "Deleted."}
MAX_PAGE = 1_000_000
MAX_QUERY = 200
OPTION_LIMIT = 200
RECENT = 5
SEARCH_HITS = 5
DASHBOARD_STATS = 6


class OutOfScope(Exception):
    """A write would leave the rows the principal is allowed to see."""


class ModelAdmin[M: Model]:
    """How one model appears in the admin. Subclass and override what you need.

    * ``queryset`` scopes every list, lookup, update and delete (it is part of
      the SQL statement); ``can`` gates ``view``, ``add``, ``change`` and
      ``delete`` for a model or a row. Row visibility belongs in ``queryset``.
    * ``save`` and ``remove`` are the single places that write, so an
      application can run a domain command there instead of writing around its
      rules. ``assign`` columns are set by the server on creation and are not
      form fields; ``readonly`` columns are shown but never accepted;
      ``exclude`` columns are neither shown nor accepted.
    * After a default write the row must still be inside ``queryset``;
      otherwise the write is rolled back.
    """

    model: type[M]
    label: ClassVar[str] = ""
    icon: ClassVar[str] = "file-text"
    columns: ClassVar[tuple[str, ...]] = ()
    search: ClassVar[tuple[str, ...]] = ()
    ordering: ClassVar[tuple[str, ...]] = ()
    exclude: ClassVar[tuple[str, ...]] = ()
    readonly: ClassVar[tuple[str, ...]] = ()
    assign: ClassVar[Mapping[str, Callable[[Any], Any]]] = {}
    badges: ClassVar[Mapping[str, Mapping[Any, Badge]]] = {}
    tabs: ClassVar[tuple[Tab, ...]] = ()
    fieldsets: ClassVar[tuple[Fieldset, ...]] = ()
    modified_field: ClassVar[str] = ""
    modified_label: ClassVar[str] = "Last modified"
    per_page: ClassVar[int] = 20
    show_count: ClassVar[bool] = False
    bulk_delete: ClassVar[bool] = False

    def queryset(self, principal: Principal[Any]) -> QuerySet[M]:  # noqa: ARG002
        return self.model.all()

    def can(
        self, principal: Principal[Any], action: str, row: M | None = None
    ) -> bool:
        return True

    def title_of(self, row: M) -> str:
        return f"{type(row).__name__} {getattr(row, self.model.__primary_key__)}"

    def display(self, row: M, column: str) -> Any:
        """The value shown in a list column (return a ``Badge`` or ``Avatar`` for
        richer cells)."""
        value = getattr(row, column)
        mapping = self.badges.get(column)
        if mapping is not None and value in mapping:
            return mapping[value]
        return value

    async def stats(self, principal: Principal[Any]) -> list[Stat]:
        total = await self.queryset(principal).acount()
        return [Stat(self.plural(), total, self.icon)]

    async def check(
        self, values: dict[str, Any], principal: Principal[Any], row: M | None
    ) -> dict[str, str]:
        """Cross-field validation hook: return ``{field: message}`` to refuse."""
        return {}

    async def save(
        self, values: dict[str, Any], principal: Principal[Any], row: M | None = None
    ) -> M:
        key = self.model.__primary_key__
        if row is None:
            made = {**values, **{c: f(principal) for c, f in self.assign.items()}}
            created = await self.model.acreate(**made)
            await self.ensure_visible(created, principal)
            return created
        if not values:
            return row
        found = (
            await self.queryset(principal)
            .filter(**{key: getattr(row, key)})
            .aupdate(values)
        )
        if not found:
            raise self.model.NotFound(f"{self.model.__name__} not found")
        await self.ensure_visible(found[0], principal)
        return found[0]

    async def remove(self, row: M, principal: Principal[Any]) -> None:
        key = self.model.__primary_key__
        gone = (
            await self.queryset(principal).filter(**{key: getattr(row, key)}).adelete()
        )
        if not gone:
            raise self.model.NotFound(f"{self.model.__name__} not found")

    async def ensure_visible(self, row: M, principal: Principal[Any]) -> None:
        key = self.model.__primary_key__
        query = self.queryset(principal).filter(**{key: getattr(row, key)})
        if not await query.aexists():
            raise OutOfScope

    def plural(self) -> str:
        return self.label or f"{self.model.__name__}s"

    def shown_columns(self) -> tuple[str, ...]:
        return self.columns or tuple(self.model.__columns__[:6])

    def skipped(self) -> frozenset[str]:
        return frozenset(self.exclude) | frozenset(self.assign)

    def validate_config(self, slug: str) -> None:
        names = set(self.model.__columns__)
        for label, used in (
            ("columns", self.columns),
            ("search", self.search),
            ("ordering", [o.lstrip("-") for o in self.ordering]),
            ("exclude", self.exclude),
            ("readonly", self.readonly),
            ("assign", self.assign),
            ("badges", self.badges),
            ("modified_field", [self.modified_field] if self.modified_field else []),
        ):
            unknown = sorted(set(used) - names)
            if unknown:
                raise RegistrationError(
                    f"admin {slug!r}: {label} names unknown columns {unknown}"
                )
        for tab in self.tabs:
            for column in tab.filters:
                self.model.check_field(column.partition("__")[0])
        for fieldset in self.fieldsets:
            unknown = sorted(set(fieldset.fields) - names)
            if unknown:
                raise RegistrationError(
                    f"admin {slug!r}: fieldset {fieldset.title!r} names {unknown}"
                )


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


@dataclasses.dataclass(frozen=True, slots=True)
class ListState:
    """The list's query string: search, page, tab, sort and the done notice."""

    q: str = ""
    page: int = 1
    view: int = 0
    sort: str = ""
    direction: str = "asc"
    done: str = ""

    @classmethod
    def from_request(cls, request: Request) -> ListState:
        get = request.GET
        return cls(
            q=get.get("q", "")[:MAX_QUERY],
            page=number(get.get("page", "1"), 1, MAX_PAGE),
            view=number(get.get("view", "0"), 0, 50),
            sort=get.get("sort", "")[:64],
            direction="desc" if get.get("dir") == "desc" else "asc",
            done=get.get("done", ""),
        )

    def query(self, **changes: Any) -> dict[str, str]:
        """Non-default parameters as strings, with ``changes`` applied."""
        state = dataclasses.replace(self, **changes)
        found = {
            "q": state.q,
            "page": str(state.page) if state.page != 1 else "",
            "view": str(state.view) if state.view else "",
            "sort": state.sort,
            "dir": state.direction if state.sort and state.direction == "desc" else "",
            "done": state.done,
        }
        return {k: v for k, v in found.items() if v}


def number(raw: str, low: int, high: int) -> int:
    try:
        value = to_int(raw)
    except ValueError:
        return low
    return max(low, min(high, value))


def principal_name(principal: Principal[Any]) -> str:
    return principal.subject


class Site:
    """One request: who is asking, which token to embed, how to answer."""

    def __init__(
        self, admin: Admin, request: Request, principal: Principal[Any]
    ) -> None:
        self.admin = admin
        self.request = request
        self.principal = principal
        self.parts = admin.parts
        self.state = ListState.from_request(request)
        self.token, self.fresh = admin.csrf.issue(request, principal.subject)

    # -- urls ---------------------------------------------------------------

    def url(self, *parts: str, **query: str) -> str:
        path = "/".join([self.admin.prefix, *[quote(p, safe="") for p in parts]])
        found = {k: v for k, v in query.items() if v}
        return path + ("?" + urlencode(found) if found else "")

    def asset(self, name: str) -> str:
        return self.url("static", name, v=self.admin.digests[name])

    def entry(self, slug: str) -> ModelAdmin[Any] | None:
        found = self.admin.models.get(slug)
        return found if found and found.can(self.principal, "view") else None

    # -- pieces of the page -------------------------------------------------

    async def nav(self, active: str) -> Raw:
        items = []
        for slug, entry in self.admin.models.items():
            if not entry.can(self.principal, "view"):
                continue
            count = (
                await entry.queryset(self.principal).acount()
                if entry.show_count
                else None
            )
            items.append(
                self.parts.nav_item(
                    self.url(slug), entry.plural(), entry.icon, count, slug == active
                )
            )
        home = self.parts.nav_item(self.url(), "Overview", "house", None, active == "")
        return Raw("\n".join([home, *items]))

    def extra_links(self) -> Raw:
        return Raw(
            "\n".join(
                self.parts.nav_item(link.href, link.label, link.icon, None, False)
                for link in self.admin.links
            )
        )

    def user_card(self) -> Raw:
        name = principal_name(self.principal)
        action = Raw("")
        if self.admin.logout_url:
            action = Raw(
                f'<a class="hn-iconbtn" href="{escape(self.admin.logout_url)}" '
                'aria-label="Sign out"><svg class="hn-icon" aria-hidden="true">'
                '<use href="#i-log-out"/></svg></a>'
            )
        return self.admin.templates.render(
            "usercard",
            avatar=Raw(
                f'<span class="hn-avatar" aria-hidden="true">{escape(initials(name))}</span>'
            ),
            name=name,
            role=self.admin.role(self.principal),
            action=action,
        )

    def topbar(self, crumbs: list[tuple[str, str]], query: str = "") -> Raw:
        parts = []
        for index, (label, href) in enumerate(crumbs):
            last = index == len(crumbs) - 1
            text = f"<strong>{escape(label)}</strong>" if last else escape(label)
            parts.append(
                f'<a href="{escape(href)}">{text}</a>' if href and not last else text
            )
        toggle = Raw(
            '<button type="button" class="hn-iconbtn" data-hn-theme-toggle '
            'aria-label="Switch light and dark colour scheme" aria-pressed="false">'
            '<svg class="hn-icon" aria-hidden="true"><use href="#i-sun"/></svg></button>'
        )
        return self.admin.templates.render(
            "topbar",
            crumbs=Raw(
                '<span class="hn-crumb-sep" aria-hidden="true">/</span>'.join(parts)
            ),
            search_action=self.url("search"),
            query=query,
            actions=toggle,
        )

    def header(self, title: str, subtitle: str = "", actions: Raw = Raw("")) -> Raw:  # noqa: B008
        return self.admin.templates.render(
            "pageheader", title=title, subtitle=subtitle, actions=actions
        )

    async def page(  # noqa: PLR0913 - one value per part of the page
        self,
        title: str,
        content: Raw,
        *,
        crumbs: list[tuple[str, str]] | None = None,
        header: Raw | None = None,
        drawer: Raw = Raw(""),  # noqa: B008
        notice: Raw = Raw(""),  # noqa: B008
        active: str = "",
        status: int = 200,
        query: str = "",
    ) -> Reply:
        admin = self.admin
        t = admin.templates
        crumb_list = crumbs or [(admin.workspace, self.url()), (title, "")]
        sidebar = t.render(
            "sidebar",
            home=self.url(),
            site=admin.site,
            tagline=admin.tagline,
            nav=await self.nav(active),
            extra=self.extra_links(),
            user=self.user_card(),
        )
        shell = t.render(
            "shell",
            edit=admin.theme.edit,
            sidebar=sidebar,
            topbar=self.topbar(crumb_list, query),
            header=header if header is not None else self.header(title),
            notice=notice,
            content=content,
            drawer=drawer,
        )
        html = t.render(
            "page",
            theme=admin.theme.name,
            title=title,
            site=admin.site,
            stylesheet=self.asset("admin.css"),
            script=self.asset("admin.js"),
            sprite=t.sprite,
            shell=shell,
        )
        headers = list(SECURITY_HEADERS)
        if self.fresh:
            headers.append(
                cookie_header(
                    self.request, self.token, admin.prefix, secure=admin.secure_cookies
                )
            )
        return Reply(status, html, headers, "text/html; charset=utf-8")

    async def problem(self, status: int, text: str) -> Reply:
        return await self.page(
            str(status), Raw(""), status=status, notice=self.parts.notice("error", text)
        )

    def csrf_ok(self) -> bool:
        return self.admin.csrf.valid(self.request, self.principal.subject)

    def done_notice(self) -> Raw:
        text = NOTICES.get(self.state.done)
        return self.parts.notice("ok", text) if text else Raw("")


# -- list pages --------------------------------------------------------------


def apply_state(
    entry: ModelAdmin[Any], query: QuerySet[Any], state: ListState
) -> QuerySet[Any]:
    if 0 < state.view <= len(entry.tabs):
        query = query.filter(**dict(entry.tabs[state.view - 1].filters))
    if state.q and entry.search:
        query = query.filter(**{f"{entry.search[0]}__contains": state.q})
    columns = set(entry.model.__columns__)
    if state.sort in columns:
        return query.order_by(("-" if state.direction == "desc" else "") + state.sort)
    if entry.ordering:
        return query.order_by(*entry.ordering)
    return query


def column_label(name: str) -> str:
    return name.replace("_", " ").capitalize()


def table_head(site: Site, slug: str, entry: ModelAdmin[Any], bulk: bool) -> Raw:
    state = site.state
    cells = []
    if bulk:
        cells.append(
            '<th class="hn-check" scope="col"><input type="checkbox" '
            'data-hn-select-all aria-label="Select all rows"></th>'
        )
    columns = set(entry.model.__columns__)
    for name in entry.shown_columns():
        label = escape(column_label(name))
        if name in columns:
            flip = "desc" if state.sort == name and state.direction == "asc" else "asc"
            href = site.url(slug, **state.query(sort=name, direction=flip, page=1))
            mark = ""
            if state.sort == name:
                arrow = "arrow-down-up"
                mark = f' <svg class="hn-icon" aria-hidden="true"><use href="#i-{arrow}"/></svg>'
            current = ""
            if state.sort == name:
                current = (
                    ' aria-sort="'
                    + ("descending" if state.direction == "desc" else "ascending")
                    + '"'
                )
            cells.append(
                f'<th scope="col"{current}><a href="{escape(href)}">{label}{mark}</a></th>'
            )
        else:
            cells.append(f'<th scope="col">{label}</th>')
    return Raw("".join(cells))


def table_rows(
    site: Site,
    slug: str,
    entry: ModelAdmin[Any],
    rows: list[Any],
    *,
    bulk: bool,
    open_key: str = "",
) -> Raw:
    columns = entry.shown_columns()
    key_name = entry.model.__primary_key__
    out = []
    for row in rows:
        key = str(getattr(row, key_name))
        cells = []
        if bulk:
            cells.append(
                '<td class="hn-check"><input type="checkbox" name="ids" '
                f'value="{escape(key)}" data-hn-select-row '
                f'aria-label="Select {escape(entry.title_of(row))}"></td>'
            )
        for index, name in enumerate(columns):
            content = site.parts.cell(entry.display(row, name))
            if index == 0 and entry.can(site.principal, "change", row):
                href = site.url(slug, key, **site.state.query(done=""))
                content = Raw(
                    f'<a class="hn-rowlink" href="{escape(href)}">{content}</a>'
                )
            cells.append(f"<td>{content}</td>")
        opened = ' class="is-open"' if key == open_key else ""
        out.append(f"<tr{opened}>" + "".join(cells) + "</tr>")
    if not out:
        span = len(columns) + (1 if bulk else 0)
        return Raw(
            f'<tr><td colspan="{span}">{site.admin.templates.render("empty", message="No rows.")}</td></tr>'
        )
    return Raw("\n".join(out))


def pagination_for(site: Site, slug: str, page: int, per_page: int, total: int) -> Raw:
    pages = -(-total // per_page)
    first = (page - 1) * per_page + 1 if total else 0
    summary = f"{first}–{min(page * per_page, total)} of {total}" if total else "0 rows"
    if pages <= 1:
        return site.parts.pagination([], summary)
    chevron_left = '<svg class="hn-icon" aria-hidden="true"><use href="#i-chevron-left"/></svg><span class="hn-visually-hidden">Previous</span>'
    chevron_right = '<svg class="hn-icon" aria-hidden="true"><use href="#i-chevron-right"/></svg><span class="hn-visually-hidden">Next</span>'
    links: list[tuple[str, str, bool]] = [
        (
            site.url(slug, **site.state.query(page=page - 1)) if page > 1 else "",
            chevron_left,
            False,
        )
    ]
    for number_ in page_window(page, pages):
        if number_ is None:
            links.append(("", "…", False))
        else:
            links.append(
                (
                    site.url(slug, **site.state.query(page=number_)),
                    str(number_),
                    number_ == page,
                )
            )
    links.append(
        (
            site.url(slug, **site.state.query(page=page + 1)) if page < pages else "",
            chevron_right,
            False,
        )
    )
    return site.parts.pagination(links, summary)


def tab_bar(site: Site, slug: str, entry: ModelAdmin[Any]) -> Raw:
    if not entry.tabs:
        return Raw("")
    labels = ["All", *[tab.label for tab in entry.tabs]]
    t = site.admin.templates
    return Raw(
        "".join(
            t.render(
                "tab",
                href=site.url(slug, **site.state.query(view=index, page=1)),
                label=label,
                selected="true" if site.state.view == index else "false",
            )
            for index, label in enumerate(labels)
        )
    )


async def list_panel(
    site: Site, slug: str, entry: ModelAdmin[Any], open_key: str = ""
) -> Raw:
    """Toolbar, table and pagination of one model's list."""
    state = site.state
    query = apply_state(entry, entry.queryset(site.principal), state)
    from hinoki.pagination import PageParams  # noqa: PLC0415 - only here

    found = await query.apaginate(PageParams(state.page, entry.per_page))
    bulk = entry.bulk_delete and entry.can(site.principal, "delete")
    t = site.admin.templates
    hidden = ""
    if state.view:
        hidden += f'<input type="hidden" name="view" value="{state.view}">'
    toolbar = t.render(
        "toolbar",
        tabs=tab_bar(site, slug, entry),
        action=site.url(slug),
        hidden=Raw(hidden),
        query=state.q,
        placeholder=f"Search {entry.plural().lower()}…"
        if entry.search
        else "Search is not set up",
        actions=Raw(""),
    )
    bulkbar = Raw(
        '<div class="hn-bulkbar" data-hn-bulkbar><span><strong data-hn-selected-count>0</strong> selected</span>'
        '<button class="hn-btn hn-danger-soft" type="submit" name="action" value="delete">Delete selected</button></div>'
        if bulk
        else ""
    )
    table = t.render(
        "table",
        bulk_action=site.url(slug, "bulk"),
        csrf=site.token,
        bulkbar=bulkbar,
        caption=f"{entry.plural()} list",
        head=table_head(site, slug, entry, bulk),
        rows=table_rows(site, slug, entry, found.items, bulk=bulk, open_key=open_key),
    )
    pager = pagination_for(site, slug, found.page, found.page_size, found.total)
    return Raw(f"{toolbar}{table}{pager}")


def list_header(site: Site, slug: str, entry: ModelAdmin[Any]) -> Raw:
    actions = Raw("")
    if entry.can(site.principal, "add"):
        href = escape(site.url(slug, "add", **site.state.query(done="")))
        actions = Raw(
            f'<a class="hn-btn hn-primary" href="{href}"><svg class="hn-icon" '
            f'aria-hidden="true"><use href="#i-plus"/></svg>New {escape(entry.model.__name__.lower())}</a>'
        )
    return site.header(entry.plural(), f"Manage {entry.plural().lower()}.", actions)


async def list_page(
    site: Site,
    slug: str,
    entry: ModelAdmin[Any],
    *,
    drawer: Raw = Raw(""),
    open_key: str = "",
    status: int = 200,
    notice: Raw | None = None,
) -> Reply:
    panel = await list_panel(site, slug, entry, open_key)
    body = site.parts.panel(f"hn-{slug}", entry.plural(), panel)
    shown_notice = notice if notice is not None else site.done_notice()
    crumbs = [
        (site.admin.workspace, site.url()),
        (entry.plural(), site.url(slug)) if drawer else (entry.plural(), ""),
    ]
    return await site.page(
        entry.plural(),
        body,
        crumbs=crumbs,
        header=list_header(site, slug, entry),
        drawer=drawer,
        notice=shown_notice,
        active=slug,
        status=status,
        query=site.state.q if not drawer else "",
    )


# -- forms -------------------------------------------------------------------


async def option_lists(
    site: Site, fields: list[FormField]
) -> tuple[list[FormField], dict[str, list[tuple[str, str]]]]:
    """Foreign keys become selects when the related table is small enough."""
    lists: dict[str, list[tuple[str, str]]] = {}
    updated = []
    for field in fields:
        target = field.references
        if target is None:
            updated.append(field)
            continue
        related = next(
            (e for e in site.admin.models.values() if e.model is target), None
        )
        query = related.queryset(site.principal) if related else target.all()
        rows = await query.limit(OPTION_LIMIT + 1).aall()
        if len(rows) > OPTION_LIMIT:
            updated.append(field)
            continue
        label = related.title_of if related else str
        lists[field.name] = [
            (str(getattr(r, target.__primary_key__)), label(r)) for r in rows
        ]
        updated.append(dataclasses.replace(field, select=True))
    return updated, lists


def render_fields(
    site: Site,
    entry: ModelAdmin[Any],
    fields: list[FormField],
    values: Mapping[str, Any],
    errors: Mapping[str, str],
    options: Mapping[str, list[tuple[str, str]]],
) -> Raw:
    t = site.admin.templates
    by_name = {f.name: f for f in fields}
    grouped = {name for fs in entry.fieldsets for name in fs.fields}

    def one(field: FormField) -> Raw:
        return render_field(
            t,
            field,
            values.get(field.name),
            errors.get(field.name),
            options.get(field.name),
        )

    rendered = [one(f) for f in fields if f.name not in grouped]
    for fieldset in entry.fieldsets:
        inside = [one(by_name[n]) for n in fieldset.fields if n in by_name]
        if not inside:
            continue
        rendered.append(
            t.render(
                "fieldset",
                title=fieldset.title,
                open=Raw("" if fieldset.collapsed else " open"),
                fields=Raw("".join(inside)),
            )
        )
    return Raw("".join(rendered))


@dataclasses.dataclass(slots=True)
class FormView:
    """What one form page shows."""

    slug: str
    entry: ModelAdmin[Any]
    row: Any
    fields: list[FormField]
    values: dict[str, Any]
    errors: dict[str, str] = dataclasses.field(default_factory=dict)
    message: str = ""
    status: int = 200


def status_badge(entry: ModelAdmin[Any], row: Any) -> Badge | None:
    for column in entry.badges:
        shown = entry.display(row, column)
        if isinstance(shown, Badge):
            return shown
    return None


async def form_response(site: Site, view: FormView) -> Reply:
    entry, slug, row = view.entry, view.slug, view.row
    fields, options = await option_lists(site, view.fields)
    t = site.admin.templates
    key = str(getattr(row, entry.model.__primary_key__)) if row is not None else ""
    action = (
        site.url(slug, key, **site.state.query(done=""))
        if row is not None
        else site.url(slug, "add", **site.state.query(done=""))
    )
    cancel = site.url(slug, **site.state.query(done=""))
    summary = Raw("")
    if view.message:
        summary = site.parts.notice("error", view.message)
    elif view.errors:
        summary = site.parts.notice("error", "Please correct the highlighted fields.")
    meta = Raw("")
    if row is not None and entry.modified_field:
        when = input_text(getattr(row, entry.modified_field)).replace("T", " ")
        meta = Raw(
            f'<p class="hn-meta"><svg class="hn-icon" aria-hidden="true"><use href="#i-clock"/></svg><span>{escape(entry.modified_label)}: {escape(when)} UTC</span></p>'
        )
    delete = Raw("")
    if row is not None and entry.can(site.principal, "delete", row):
        href = escape(site.url(slug, key, "delete", **site.state.query(done="")))
        delete = Raw(
            f'<a class="hn-btn hn-danger-soft hn-delete" href="{href}" aria-label="Delete">'
            '<svg class="hn-icon" aria-hidden="true"><use href="#i-trash-2"/></svg></a>'
        )
    form = t.render(
        "form",
        action=action,
        csrf=site.token,
        summary=summary,
        fields=render_fields(site, entry, fields, view.values, view.errors, options),
        meta=meta,
        delete=delete,
        cancel=cancel,
        submit="Save changes" if row is not None else "Create",
    )
    title = (
        entry.title_of(row)
        if row is not None
        else f"New {entry.model.__name__.lower()}"
    )
    badge = status_badge(entry, row) if row is not None else None
    if site.admin.theme.edit == "drawer":
        drawer = t.render(
            "drawer",
            title=title,
            close=cancel,
            badge=site.parts.badge(badge) if badge else Raw(""),
            actions=Raw(""),
            form=form,
        )
        return await list_page(
            site,
            slug,
            entry,
            drawer=drawer,
            open_key=key,
            status=view.status,
            notice=Raw(""),
        )
    body = Raw(f'<div class="hn-card hn-page-form">{form}</div>')
    crumbs = [
        (site.admin.workspace, site.url()),
        (entry.plural(), site.url(slug)),
        (title, ""),
    ]
    return await site.page(
        title,
        body,
        crumbs=crumbs,
        header=site.header(title, ""),
        active=slug,
        status=view.status,
    )


def form_fields(entry: ModelAdmin[Any], *, creating: bool) -> list[FormField]:
    """On creation the server-assigned columns are not shown; when editing they
    are shown read-only."""
    skip = frozenset(entry.exclude) | (frozenset(entry.assign) if creating else frozenset())
    readonly = frozenset(entry.readonly) | frozenset(entry.assign)
    return fields_for(entry.model, skip, readonly, creating=creating)


async def guarded(operation: Awaitable[Any]) -> str:
    """Run a write in its own savepoint so a refused statement rolls back alone;
    returns a message for the form, or ``""`` on success."""
    try:
        async with current_database().transaction():
            await operation
    except UniqueViolation:
        return "That value is already in use."
    except ForeignKeyViolation:
        return "That refers to, or is used by, another row."
    except OutOfScope:
        return "That change would move the row out of the rows you may manage."
    except (NotNullViolation, CheckViolation, DatabaseError):
        return "The database rejected these values."
    return ""


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


# -- views -------------------------------------------------------------------


async def stylesheet(self: API, request: Request) -> Reply:  # noqa: ARG001
    admin: Admin = self.admin  # type: ignore[attr-defined]
    return Reply(
        200,
        admin.templates.stylesheet(),
        [
            ("cache-control", "public, max-age=31536000, immutable"),
            ("x-content-type-options", "nosniff"),
        ],
        "text/css; charset=utf-8",
    )


async def script(self: API, request: Request) -> Reply:  # noqa: ARG001
    admin: Admin = self.admin  # type: ignore[attr-defined]
    return Reply(
        200,
        admin.templates.script(),
        [
            ("cache-control", "public, max-age=31536000, immutable"),
            ("x-content-type-options", "nosniff"),
        ],
        "text/javascript; charset=utf-8",
    )


async def index(self: API, request: Request, principal: Principal[Any]) -> Reply:
    site = Site(self.admin, request, principal)  # type: ignore[attr-defined]
    admin = site.admin
    stats: list[Stat] = []
    for entry in admin.models.values():
        if entry.can(principal, "view"):
            stats.extend(await entry.stats(principal))
    cards = Raw("".join(admin.parts.stat(s) for s in stats[:DASHBOARD_STATS]))
    blocks = [admin.templates.render("stats", cards=cards)] if stats else []
    slugs = admin.dashboard or tuple(list(admin.models)[:1])
    for slug in slugs:
        entry = site.entry(slug)
        if entry is None:
            continue
        rows = (
            await apply_state(entry, entry.queryset(principal), ListState())
            .limit(RECENT)
            .aall()
        )
        table = admin.templates.render(
            "table",
            bulk_action=site.url(slug, "bulk"),
            csrf=site.token,
            bulkbar=Raw(""),
            caption=f"Recent {entry.plural().lower()}",
            head=table_head(site, slug, entry, False),
            rows=table_rows(site, slug, entry, rows, bulk=False),
        )
        link = Raw(f'<a class="hn-btn" href="{escape(site.url(slug))}">View all</a>')
        blocks.append(
            admin.parts.panel(
                f"hn-recent-{slug}", f"Recent {entry.plural().lower()}", table, link
            )
        )
    if admin.activity is not None:
        events = await admin.activity(principal)
        if events:
            items = Raw(
                "".join(
                    admin.templates.render(
                        "event",
                        text=e.text,
                        when=e.when,
                        icon=icon_name(e.icon, "pencil"),
                        tone=e.tone if e.tone in {"ok", "warn", "danger"} else "accent",
                    )
                    for e in events
                )
            )
            blocks.append(
                admin.parts.panel(
                    "hn-activity",
                    "Recent activity",
                    admin.templates.render("activity", items=items),
                )
            )
    return await site.page(
        "Overview",
        Raw("".join(blocks)),
        header=site.header("Overview", f"Manage {admin.site.lower()}."),
        active="",
        notice=site.done_notice(),
    )


async def search(self: API, request: Request, principal: Principal[Any]) -> Reply:
    site = Site(self.admin, request, principal)  # type: ignore[attr-defined]
    text = request.GET.get("q", "")[:MAX_QUERY].strip()
    groups = []
    if text:
        for slug, entry in site.admin.models.items():
            if not entry.search or not entry.can(principal, "view"):
                continue
            query = entry.queryset(principal).filter(
                **{f"{entry.search[0]}__contains": text}
            )
            rows = await query.limit(SEARCH_HITS).aall()
            if rows:
                links = "".join(
                    f'<li><a href="{escape(site.url(slug, str(getattr(r, entry.model.__primary_key__))))}">{escape(entry.title_of(r))}</a></li>'
                    for r in rows
                    if entry.can(principal, "change", r)
                )
                groups.append(f"<h3>{escape(entry.plural())}</h3><ul>{links}</ul>")
    body = (
        Raw("".join(groups))
        if groups
        else site.admin.templates.render(
            "empty", message="No results." if text else "Type something to search."
        )
    )
    panel = site.parts.panel(
        "hn-results", "Results", Raw(f'<div class="hn-results">{body}</div>')
    )
    return await site.page(
        "Search",
        panel,
        crumbs=[(site.admin.workspace, site.url()), ("Search", "")],
        header=site.header("Search", text and f"Matches for “{text}”."),
        query=text,
    )


async def listing(
    self: API, request: Request, principal: Principal[Any], model: str
) -> Reply:
    site = Site(self.admin, request, principal)  # type: ignore[attr-defined]
    entry = site.entry(model)
    if entry is None:
        return await site.problem(404, "No such model.")
    return await list_page(site, model, entry)


async def add_form(
    self: API, request: Request, principal: Principal[Any], model: str
) -> Reply:
    site = Site(self.admin, request, principal)  # type: ignore[attr-defined]
    entry = site.entry(model)
    if entry is None or not entry.can(principal, "add"):
        return await site.problem(404, "No such model.")
    fields = form_fields(entry, creating=True)
    return await form_response(site, FormView(model, entry, None, fields, {}))


async def add_submit(
    self: API, request: Request, principal: Principal[Any], model: str
) -> Reply:
    site = Site(self.admin, request, principal)  # type: ignore[attr-defined]
    entry = site.entry(model)
    if entry is None or not entry.can(principal, "add"):
        return await site.problem(404, "No such model.")
    if not site.csrf_ok():
        return await site.problem(
            403, "The form expired or is invalid. Reload and try again."
        )
    fields = form_fields(entry, creating=True)
    values, errors = collect(fields, request.POST)
    errors = errors or await entry.check(values, principal, None)
    message = "" if errors else await guarded(entry.save(values, principal))
    if errors or message:
        view = FormView(
            model, entry, None, fields, dict(request.POST), errors, message, 422
        )
        return await form_response(site, view)
    return Reply(
        303, headers=[("location", site.url(model, **site.state.query(done="created")))]
    )


async def open_row(
    site: Site, model: str, key: str, action: str
) -> tuple[ModelAdmin[Any] | None, Any]:
    entry = site.entry(model)
    row = await find_row(site, entry, key) if entry else None
    allowed = (
        entry is not None
        and row is not None
        and entry.can(site.principal, "view", row)
        and entry.can(site.principal, action, row)
    )
    return (entry, row) if allowed else (None, None)


async def edit_form(
    self: API, request: Request, principal: Principal[Any], model: str, key: str
) -> Reply:
    site = Site(self.admin, request, principal)  # type: ignore[attr-defined]
    entry, row = await open_row(site, model, key, "change")
    if entry is None:
        return await site.problem(404, "Not found.")
    fields = form_fields(entry, creating=False)
    values = {f.name: getattr(row, f.name) for f in fields}
    return await form_response(site, FormView(model, entry, row, fields, values))


async def edit_submit(
    self: API, request: Request, principal: Principal[Any], model: str, key: str
) -> Reply:
    site = Site(self.admin, request, principal)  # type: ignore[attr-defined]
    entry, row = await open_row(site, model, key, "change")
    if entry is None:
        return await site.problem(404, "Not found.")
    if not site.csrf_ok():
        return await site.problem(
            403, "The form expired or is invalid. Reload and try again."
        )
    fields = form_fields(entry, creating=False)
    original = {f.name: getattr(row, f.name) for f in fields}
    values, errors = collect(fields, request.POST, original)
    errors = errors or await entry.check(values, principal, row)
    message = "" if errors else await guarded(entry.save(values, principal, row))
    if errors or message:
        view = FormView(
            model, entry, row, fields, dict(request.POST), errors, message, 422
        )
        return await form_response(site, view)
    return Reply(
        303, headers=[("location", site.url(model, **site.state.query(done="saved")))]
    )


async def delete_form(
    self: API, request: Request, principal: Principal[Any], model: str, key: str
) -> Reply:
    site = Site(self.admin, request, principal)  # type: ignore[attr-defined]
    entry, row = await open_row(site, model, key, "delete")
    if entry is None:
        return await site.problem(404, "Not found.")
    content = site.admin.templates.render(
        "confirm",
        action=site.url(model, key, "delete", **site.state.query(done="")),
        csrf=site.token,
        hidden=Raw(""),
        message=f"Delete {entry.title_of(row)}? This cannot be undone.",
        cancel=site.url(model, **site.state.query(done="")),
    )
    return await site.page(
        "Delete",
        content,
        crumbs=[
            (site.admin.workspace, site.url()),
            (entry.plural(), site.url(model)),
            ("Delete", ""),
        ],
        active=model,
    )


async def delete_submit(
    self: API, request: Request, principal: Principal[Any], model: str, key: str
) -> Reply:
    site = Site(self.admin, request, principal)  # type: ignore[attr-defined]
    entry, row = await open_row(site, model, key, "delete")
    if entry is None:
        return await site.problem(404, "Not found.")
    if not site.csrf_ok():
        return await site.problem(
            403, "The form expired or is invalid. Reload and try again."
        )
    message = await guarded(entry.remove(row, principal))
    if message:
        return await site.problem(409, message)
    return Reply(
        303, headers=[("location", site.url(model, **site.state.query(done="deleted")))]
    )


async def bulk(
    self: API, request: Request, principal: Principal[Any], model: str
) -> Reply:
    site = Site(self.admin, request, principal)  # type: ignore[attr-defined]
    entry = site.entry(model)
    if entry is None or not entry.bulk_delete or not entry.can(principal, "delete"):
        return await site.problem(404, "No such model.")
    if not site.csrf_ok():
        return await site.problem(
            403, "The form expired or is invalid. Reload and try again."
        )
    chosen = request.POST.getlist("ids")[:OPTION_LIMIT]
    rows = []
    for raw in chosen:
        row = await find_row(site, entry, raw)
        if row is not None and entry.can(principal, "delete", row):
            rows.append(row)
    if not rows:
        return Reply(303, headers=[("location", site.url(model))])
    if request.POST.get("confirmed") != "1":
        hidden = Raw(
            '<input type="hidden" name="confirmed" value="1">'
            + "".join(
                f'<input type="hidden" name="ids" value="{escape(getattr(r, entry.model.__primary_key__))}">'
                for r in rows
            )
        )
        content = site.admin.templates.render(
            "confirm",
            action=site.url(model, "bulk"),
            csrf=site.token,
            hidden=hidden,
            message=f"Delete {len(rows)} selected {entry.plural().lower()}? This cannot be undone.",
            cancel=site.url(model),
        )
        return await site.page("Delete selected", content, active=model)
    failed = ""
    for row in rows:
        failed = failed or await guarded(entry.remove(row, principal))
    if failed:
        return await site.problem(409, failed)
    return Reply(303, headers=[("location", site.url(model, done="deleted"))])


VIEWS: dict[str, Callable[..., Any]] = {
    "stylesheet": stylesheet,
    "script": script,
    "index": index,
    "search": search,
    "listing": listing,
    "add_form": add_form,
    "add_submit": add_submit,
    "edit_form": edit_form,
    "edit_submit": edit_submit,
    "delete_form": delete_form,
    "delete_submit": delete_submit,
    "bulk": bulk,
}
ROUTES: list[tuple[str, str, str, bool]] = [
    ("stylesheet", "GET", "/static/admin.css", False),
    ("script", "GET", "/static/admin.js", False),
    ("index", "GET", "/", False),
    ("search", "GET", "/search", False),
    ("listing", "GET", "/{model}", False),
    ("add_form", "GET", "/{model}/add", False),
    ("add_submit", "POST", "/{model}/add", True),
    ("bulk", "POST", "/{model}/bulk", True),
    ("edit_form", "GET", "/{model}/{key}", False),
    ("edit_submit", "POST", "/{model}/{key}", True),
    ("delete_form", "GET", "/{model}/{key}/delete", False),
    ("delete_submit", "POST", "/{model}/{key}/delete", True),
]

__all__ = [
    "Admin",
    "Avatar",
    "Badge",
    "Event",
    "Fieldset",
    "Link",
    "ModelAdmin",
    "Stat",
    "Tab",
]
