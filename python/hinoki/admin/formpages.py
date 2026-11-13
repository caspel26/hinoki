"""Form pages: fields, fieldsets, selects and the edit drawer or page."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from hinoki.admin.forms import (
    FormField,
    fields_for,
    input_text,
    render_field,
)
from hinoki.admin.lists import list_page
from hinoki.admin.modeladmin import ModelAdmin
from hinoki.admin.parts import Badge
from hinoki.admin.site import OPTION_LIMIT, Site
from hinoki.admin.templates import Raw, escape
from hinoki.reply import Reply


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
    skip = frozenset(entry.exclude) | (
        frozenset(entry.assign) if creating else frozenset()
    )
    readonly = frozenset(entry.readonly) | frozenset(entry.assign)
    return fields_for(entry.model, skip, readonly, creating=creating)
