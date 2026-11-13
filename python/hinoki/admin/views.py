"""The admin's route handlers and the route table."""

from __future__ import annotations

import typing
from collections.abc import Callable
from typing import Any

from hinoki.admin.components import icon_name
from hinoki.admin.formpages import (
    FormView,
    check_references,
    form_fields,
    form_response,
)
from hinoki.admin.forms import (
    collect,
)
from hinoki.admin.lists import apply_state, list_page, table_head, table_rows
from hinoki.admin.modeladmin import ModelAdmin
from hinoki.admin.parts import Avatar, Badge, Event, Fieldset, Link, Stat, Tab
from hinoki.admin.site import (
    DASHBOARD_STATS,
    MAX_QUERY,
    OPTION_LIMIT,
    RECENT,
    SEARCH_HITS,
    ListState,
    Site,
    find_row,
    guarded,
)
from hinoki.admin.templates import Raw, escape
from hinoki.api import API
from hinoki.auth import Caller
from hinoki.reply import Reply
from hinoki.request import Request

if typing.TYPE_CHECKING:
    from hinoki.admin.core import Admin
    from hinoki.admin.modeladmin import ModelAdmin


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


async def index(self: API, request: Request, principal: Caller[Any]) -> Reply:
    site = Site(self.admin, request, principal)  # type: ignore[attr-defined]
    admin = site.admin
    stats: list[Stat] = []
    for each in admin.models.values():
        if each.can(principal, "view"):
            stats.extend(await each.stats(principal))
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
        header=site.header("Overview", f"Welcome to {admin.site}."),
        active="",
        notice=site.done_notice(),
    )


async def search(self: API, request: Request, principal: Caller[Any]) -> Reply:
    site = Site(self.admin, request, principal)  # type: ignore[attr-defined]
    text = request.GET.get("q", "").replace("\x00", "")[:MAX_QUERY].strip()
    groups = []
    if text:
        for slug, entry in site.admin.models.items():
            if not entry.search or not entry.can(principal, "view"):
                continue
            query = entry.queryset(principal).filter(
                **{f"{entry.search[0]}__contains": text}
            )
            rows = await query.limit(SEARCH_HITS).aall()
            links = "".join(
                f'<li><a href="{escape(site.url(slug, str(getattr(r, entry.model.__primary_key__))))}">{escape(entry.title_of(r))}</a></li>'
                for r in rows
                if entry.can(principal, "view", r) and entry.can(principal, "change", r)
            )
            if links:
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
    self: API, request: Request, principal: Caller[Any], model: str
) -> Reply:
    site = Site(self.admin, request, principal)  # type: ignore[attr-defined]
    entry = site.entry(model)
    if entry is None:
        return await site.problem(404, "No such model.")
    return await list_page(site, model, entry)


async def add_form(
    self: API, request: Request, principal: Caller[Any], model: str
) -> Reply:
    site = Site(self.admin, request, principal)  # type: ignore[attr-defined]
    entry = site.entry(model)
    if entry is None or not entry.can(principal, "add"):
        return await site.problem(404, "No such model.")
    fields = form_fields(entry, creating=True)
    return await form_response(site, FormView(model, entry, None, fields, {}))


async def add_submit(
    self: API, request: Request, principal: Caller[Any], model: str
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
    errors = errors or await check_references(site, fields, values)
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
    self: API, request: Request, principal: Caller[Any], model: str, key: str
) -> Reply:
    site = Site(self.admin, request, principal)  # type: ignore[attr-defined]
    entry, row = await open_row(site, model, key, "change")
    if entry is None:
        return await site.problem(404, "Not found.")
    fields = form_fields(entry, creating=False)
    values = {f.name: getattr(row, f.name) for f in fields}
    return await form_response(site, FormView(model, entry, row, fields, values))


async def edit_submit(
    self: API, request: Request, principal: Caller[Any], model: str, key: str
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
    errors = errors or await check_references(site, fields, values)
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
    self: API, request: Request, principal: Caller[Any], model: str, key: str
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
    self: API, request: Request, principal: Caller[Any], model: str, key: str
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


async def remove_all(
    entry: ModelAdmin[Any], rows: list[Any], principal: Caller[Any]
) -> None:
    """All deletions in one savepoint: either every row goes or none does."""
    for row in rows:
        await entry.remove(row, principal)


async def bulk(
    self: API, request: Request, principal: Caller[Any], model: str
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
        if (
            row is not None
            and entry.can(principal, "view", row)
            and entry.can(principal, "delete", row)
        ):
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
    failed = await guarded(remove_all(entry, rows, principal))
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
