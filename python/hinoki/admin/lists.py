"""List pages: tabs, table, pagination and the toolbar."""

from __future__ import annotations

from typing import Any, cast

from hinoki.admin.components import page_window
from hinoki.admin.forms import (
    label_of,
)
from hinoki.admin.modeladmin import ModelAdmin
from hinoki.admin.site import MAX_PAGE, ListState, Site  # noqa: F401
from hinoki.admin.templates import Raw, escape
from hinoki.http.response import Response
from hinoki.orm import QuerySet

NO_HTML = Raw("")


def matching(entry: ModelAdmin[Any], query: QuerySet[Any], text: str) -> QuerySet[Any]:
    """Rows where any ``search`` column contains ``text`` (case-insensitive)."""
    if len(entry.search) == 1:
        return query.filter(**{f"{entry.search[0]}__contains": text})
    # The queryset has no public OR; the compiler takes the columns as one filter.
    added = (",".join(entry.search), "containsany", [text])
    return query.derive(filters=(*query.filters, added))


def apply_state(
    entry: ModelAdmin[Any], query: QuerySet[Any], state: ListState
) -> QuerySet[Any]:
    if 0 < state.view <= len(entry.tab_list):
        query = query.filter(**dict(entry.tab_list[state.view - 1].filters))
    if state.q and entry.search:
        query = matching(entry, query, state.q)
    columns = set(entry.model.__columns__) & set(entry.shown_columns())
    if state.sort in columns:
        return query.order_by(("-" if state.direction == "desc" else "") + state.sort)
    if entry.ordering:
        return query.order_by(*entry.ordering)
    return query


def column_label(name: str) -> str:
    return label_of(name)


def _heading_for_column(site: Site, slug: str, name: str, label: str) -> str:
    state = site.state
    flip = "desc" if state.sort == name and state.direction == "asc" else "asc"
    href = site.url(slug, **state.query(sort=name, direction=flip, page=1))
    mark = (
        ' <svg class="hn-icon" aria-hidden="true"><use href="#i-arrow-down-up"/></svg>'
        if state.sort == name
        else ""
    )
    current = ""
    if state.sort == name:
        direction = "descending" if state.direction == "desc" else "ascending"
        current = f' aria-sort="{direction}"'
    return f'<th scope="col"{current}><a href="{escape(href)}">{label}{mark}</a></th>'


def table_head(site: Site, slug: str, entry: ModelAdmin[Any], bulk: bool) -> Raw:
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
            cells.append(_heading_for_column(site, slug, name, label))
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
    # A row the caller may not view is not listed. This filters the rows fetched for
    # this page only; counts and page numbers still come from ``queryset()``.
    for row in (r for r in rows if entry.can(site.principal, "view", r)):
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
            if (
                index == 0
                and not row.is_deleted
                and entry.can(site.principal, "change", row)
            ):
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
    summary = (
        f"{first}\u2013{min(page * per_page, total)} of {total}" if total else "0 rows"
    )
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
    if entry.model.__soft_delete__ is not None and not entry.tabs:
        return Raw(
            "".join(
                site.admin.templates.render(
                    "tab",
                    href=site.url(slug, **site.state.query(deleted=deleted, page=1)),
                    label=label,
                    selected="true" if site.state.deleted is deleted else "false",
                )
                for label, deleted in (("Active", False), ("Deleted", True))
            )
        )
    if not entry.tab_list:
        return Raw("")
    labels = ["All", *[tab.label for tab in entry.tab_list]]
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
    query = apply_state(entry, list_scope(site, entry), state)
    from hinoki.pagination import PageParams  # noqa: PLC0415 - only here

    found = await query.apaginate(PageParams(state.page, cast(int, entry.per_page)))
    bulk_delete, actions = bulk_controls(site, entry)
    bulk = bulk_delete or bool(actions)
    toolbar = render_toolbar(site, slug, entry)
    bulkbar = render_bulkbar(bulk_delete, actions, bulk)
    table = site.admin.templates.render(
        "table",
        bulk_action=site.url(slug, "bulk", **state.query(done="")),
        csrf=site.token,
        bulkbar=bulkbar,
        caption=f"{entry.plural()} list",
        head=table_head(site, slug, entry, bulk),
        rows=table_rows(site, slug, entry, found.items, bulk=bulk, open_key=open_key),
    )
    pager = pagination_for(site, slug, found.page, found.page_size, found.total)
    return Raw(f"{toolbar}{table}{pager}")


def list_scope(site: Site, entry: ModelAdmin[Any]) -> QuerySet[Any]:
    """The active or deleted row scope selected by the list query string."""
    if site.state.deleted and entry.model.__soft_delete__ is not None:
        return entry.deleted_scope(site.principal)
    return entry.active_scope(site.principal)


def bulk_controls(
    site: Site, entry: ModelAdmin[Any]
) -> tuple[bool, list[tuple[str, str]]]:
    """Actions visible for the current active/deleted list scope."""
    state = site.state
    bulk_delete = (
        not state.deleted and entry.bulk_delete and entry.can(site.principal, "delete")
    )
    actions = [
        (action.name, action.label)
        for action in entry.bulk_actions
        if not state.deleted and entry.can(site.principal, action.name)
    ]
    if state.deleted and entry.model.__soft_delete__ is not None:
        if entry.can(site.principal, "change"):
            actions.append(("restore", "Restore selected"))
        if entry.allow_hard_delete and entry.can(site.principal, "delete"):
            actions.append(("hard-delete", "Hard-delete selected"))
    return bulk_delete, actions


def render_toolbar(site: Site, slug: str, entry: ModelAdmin[Any]) -> Raw:
    """The search toolbar with the current view carried by its form."""
    state = site.state
    hidden = ""
    if state.view:
        hidden += f'<input type="hidden" name="view" value="{state.view}">'
    if state.deleted:
        hidden += '<input type="hidden" name="deleted" value="1">'
    placeholder = (
        f"Search {entry.plural().lower()}…" if entry.search else "Search is not set up"
    )
    return site.admin.templates.render(
        "toolbar",
        tabs=tab_bar(site, slug, entry),
        action=site.url(slug),
        hidden=Raw(hidden),
        query=state.q,
        placeholder=placeholder,
        actions=Raw(""),
    )


def render_bulkbar(
    bulk_delete: bool, actions: list[tuple[str, str]], visible: bool
) -> Raw:
    """The buttons for delete, restore and registered bulk actions."""
    if not visible:
        return Raw("")
    buttons = []
    if bulk_delete:
        buttons.append(
            '<button class="hn-btn hn-danger-soft" type="submit" '
            'name="action" value="delete">Delete selected</button>'
        )
    for name, label in actions:
        buttons.append(
            '<button class="hn-btn" type="submit" name="action" '
            f'value="{escape(name)}">{escape(label)}</button>'
        )
    return Raw(
        '<div class="hn-bulkbar" data-hn-bulkbar>'
        "<span><strong data-hn-selected-count>0</strong> selected</span>"
        + "".join(buttons)
        + "</div>"
    )


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
    drawer: Raw = NO_HTML,
    open_key: str = "",
    status: int = 200,
    notice: Raw | None = None,
) -> Response:
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
