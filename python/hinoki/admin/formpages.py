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
from hinoki.admin.linkfields import link_form_fields, link_lists
from hinoki.admin.lists import list_page
from hinoki.admin.modeladmin import ModelAdmin
from hinoki.admin.parts import Badge
from hinoki.admin.site import OPTION_LIMIT, Site
from hinoki.admin.templates import Raw, escape
from hinoki.http.request import Request
from hinoki.http.response import Response

COLLECTIONS = frozenset({"choices", "links"})


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
        if related is None or not related.can(site.principal, "view"):
            # No admin for the target, or the caller may not view it: never list
            # its rows. The field stays a plain id input.
            updated.append(field)
            continue
        rows = await related.queryset(site.principal).limit(OPTION_LIMIT + 1)
        if len(rows) > OPTION_LIMIT:
            updated.append(field)
            continue
        lists[field.name] = [
            (str(getattr(r, target.__primary_key__)), related.title_of(r)) for r in rows
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
    grouped = {name for fs in entry.fieldset_list for name in fs.fields}

    def one(field: FormField) -> Raw:
        return render_field(
            t,
            field,
            values.get(field.name),
            errors.get(field.name),
            options.get(field.name),
            site.admin.timezone,
        )

    rendered = [one(f) for f in fields if f.name not in grouped]
    for fieldset in entry.fieldset_list:
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


async def form_response(site: Site, view: FormView) -> Response:
    entry, slug, row = view.entry, view.slug, view.row
    fields, options = await option_lists(site, view.fields)
    fields, link_options = await link_lists(site, entry, fields, view.values)
    options.update(link_options)
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
    stamp = getattr(row, entry.modified_field, None) if entry.modified_field else None
    if row is not None and stamp is not None:
        when = input_text(stamp, site.admin.timezone).replace("T", " ")
        meta = Raw(
            f'<p class="hn-meta"><svg class="hn-icon" aria-hidden="true"><use href="#i-clock"/></svg><span>{escape(entry.modified_label)}: {escape(when)} {escape(str(site.admin.timezone))}</span></p>'
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
    columns = fields_for(entry.model, skip, readonly, creating=creating)
    extras = [
        FormField(
            extra.name,
            extra.label,
            extra.kind,
            False,
            False,
            True,
            None,
            str,
            virtual=True,
            choices=extra.choices,
            note=extra.note,
        )
        for extra in entry.extras()
    ]
    return [*columns, *link_form_fields(entry), *extras]


async def current_values(
    entry: ModelAdmin[Any], row: Any, fields: list[FormField]
) -> dict[str, Any]:
    """What the edit form shows: the row's columns, then each extra's initial."""
    extra = await entry.extra_initial(row)
    return {
        f.name: extra.get(f.name, frozenset() if f.kind in COLLECTIONS else "")
        if f.virtual
        else getattr(row, f.name)
        for f in fields
    }


def submitted(request: Request, fields: list[FormField]) -> dict[str, Any]:
    """The posted values for a form shown again, a set of choices kept whole."""
    shown: dict[str, Any] = dict(request.POST)
    for field in fields:
        if field.kind in COLLECTIONS:
            shown[field.name] = frozenset(request.POST.getlist(field.name))
    return shown


async def check_references(
    site: Site, fields: list[FormField], values: dict[str, Any]
) -> dict[str, str]:
    """A foreign key must point at a row the caller may see in the related
    admin; the select only limits the browser, this limits the server."""
    errors: dict[str, str] = {}
    for field in fields:
        target = field.references
        value = values.get(field.name)
        if target is None or value is None or field.shown_only:
            continue
        related = next(
            (e for e in site.admin.models.values() if e.model is target), None
        )
        if related is None:
            continue
        query = related.queryset(site.principal).filter(
            **{target.__primary_key__: value}
        )
        if not related.can(site.principal, "view") or not await query.aexists():
            errors[field.name] = "That choice is not available."
    return errors
