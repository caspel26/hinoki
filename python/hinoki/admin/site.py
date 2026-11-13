"""One admin request: urls, navigation, page chrome and the shared helpers."""

from __future__ import annotations

import dataclasses
import typing
from collections.abc import Awaitable
from typing import Any
from urllib.parse import quote, urlencode

from hinoki.admin.components import initials
from hinoki.admin.csrf import cookie_header
from hinoki.admin.modeladmin import ModelAdmin, OutOfScope
from hinoki.admin.templates import Raw, escape
from hinoki.auth import Principal
from hinoki.db import (
    CheckViolation,
    DatabaseError,
    ForeignKeyViolation,
    NotNullViolation,
    UniqueViolation,
)
from hinoki.orm import current_database
from hinoki.reply import Reply
from hinoki.request import Request
from hinoki.runtime.binding import to_int

if typing.TYPE_CHECKING:
    from hinoki.admin.core import Admin
    from hinoki.admin.modeladmin import ModelAdmin

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
