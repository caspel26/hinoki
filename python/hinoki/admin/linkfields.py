"""Many-to-many fields of the admin forms: a bounded multi-select.

The choices are the rows of the target's admin, narrowed by its ``queryset``;
a caller who may not view the target sees the field read-only, without a count.
Choices and the search are bounded, and a write replaces only the links inside
that scope."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from typing import Any, cast

from hinoki import core
from hinoki.admin.forms import LINK_REFUSAL, FormField, label_of
from hinoki.admin.lists import matching
from hinoki.admin.modeladmin import ModelAdmin, OutOfScope
from hinoki.admin.parts import Refusal
from hinoki.admin.site import OPTION_LIMIT, Site
from hinoki.errors import ModelValidationError
from hinoki.orm import QuerySet
from hinoki.orm.links import LinkSide

SEARCH_PARAM = "link_q_"
SEARCH_LENGTH = 100


def side_of(entry: ModelAdmin[Any], name: str) -> LinkSide:
    """The forward relation whose ids field is ``name``."""
    return cast(
        LinkSide,
        next(
            side
            for side in entry.model.__links__.values()
            if side.forward and side.ids_name == name
        ),
    )


def link_form_fields(entry: ModelAdmin[Any]) -> list[FormField]:
    """One field per forward many-to-many relation the admin does not exclude."""
    skipped = entry.skipped()
    return [
        FormField(
            side.ids_name,
            label_of(side.declared),
            "links",
            False,
            False,
            True,
            side.max_items,
            set,
            virtual=True,
            shown_only=side.ids_name in entry.readonly,
        )
        for side in entry.model.__links__.values()
        if side.forward and side.ids_name and side.ids_name not in skipped
    ]


def target_admin(site: Site, side: LinkSide) -> ModelAdmin[Any] | None:
    """The target's admin, when the caller may view it: its rows are never
    listed or linked otherwise."""
    related = next(
        (e for e in site.admin.models.values() if e.model is side.target), None
    )
    if related is None or not related.can(site.principal, "view"):
        return None
    return related


def lock_unviewable(
    site: Site, entry: ModelAdmin[Any], fields: list[FormField]
) -> list[FormField]:
    """A field whose target the caller may not view is read-only: a posted
    value for it is ignored."""
    return [
        dataclasses.replace(field, shown_only=True)
        if field.kind == "links"
        and target_admin(site, side_of(entry, field.name)) is None
        else field
        for field in fields
    ]


def scope_of(site: Site, side: LinkSide) -> QuerySet[Any] | None:
    related = target_admin(site, side)
    return None if related is None else related.queryset(site.principal)


def valid_keys(side: LinkSide, raw: Iterable[str]) -> list[Any]:
    """The keys that parse as the target's primary key; the rest are dropped."""
    result = core.coerce_link_keys(side.spec(), list(raw), True)
    keys = result[1] if len(result) > 1 else None
    return list(keys) if result[0] == "ok" and isinstance(keys, list) else []


async def load_links(
    site: Site,
    entry: ModelAdmin[Any],
    row: Any,
    fields: list[FormField],
    values: dict[str, Any],
) -> None:
    """Put each field's linked keys, inside the caller's scope, in ``values``."""
    for field in fields:
        if field.kind != "links":
            continue
        side = side_of(entry, field.name)
        scope = scope_of(site, side)
        found = (
            []
            if scope is None
            else await getattr(row, side.declared)
            .query(within=scope)
            .limit(side.max_items)
        )
        key = side.target.__primary_key__
        values[field.name] = frozenset(str(getattr(item, key)) for item in found)


async def choices_of(
    site: Site,
    related: ModelAdmin[Any],
    side: LinkSide,
    selected: Iterable[str],
    search: str,
) -> tuple[list[tuple[str, str]], bool]:
    """The chosen rows first, then at most ``OPTION_LIMIT`` others (matching
    ``search`` when given); the flag says there were more."""
    scope = related.queryset(site.principal)
    key = side.target.__primary_key__
    chosen = valid_keys(side, selected)
    rows = (
        await scope.filter(**{f"{key}__in": chosen}).limit(side.max_items)
        if chosen
        else []
    )
    query = scope
    if search and related.search:
        query = matching(related, scope, search)
    elif search:
        query = scope.filter(**{f"{key}__in": valid_keys(side, [search])})
    ordered = query.order_by(*(related.ordering or (key,)))
    more = await ordered.limit(OPTION_LIMIT + 1)
    shown = {getattr(item, key): item for item in [*rows, *more[:OPTION_LIMIT]]}
    options = [(str(k), related.title_of(item)) for k, item in shown.items()]
    return options, len(more) > OPTION_LIMIT


async def link_lists(
    site: Site,
    entry: ModelAdmin[Any],
    fields: list[FormField],
    values: Mapping[str, Any],
) -> tuple[list[FormField], dict[str, list[tuple[str, str]]]]:
    """The options of every many-to-many field, or a read-only field (no count,
    which would leak rows outside the scope) when the caller may not view the
    target."""
    lists: dict[str, list[tuple[str, str]]] = {}
    updated = []
    for field in fields:
        if field.kind != "links":
            updated.append(field)
            continue
        side = side_of(entry, field.name)
        related = target_admin(site, side)
        if related is None:
            note = f"You may not view {side.target.__name__} rows."
            updated.append(dataclasses.replace(field, shown_only=True, note=note))
            continue
        search = site.request.GET.get(SEARCH_PARAM + field.name, "")[:SEARCH_LENGTH]
        selected = values.get(field.name) or ()
        lists[field.name], more = await choices_of(
            site, related, side, selected, search
        )
        note = (
            f"Showing {OPTION_LIMIT} choices of more; narrow them with "
            f"?{SEARCH_PARAM}{field.name}=text in the address."
            if more
            else ""
        )
        updated.append(dataclasses.replace(field, note=note))
    return updated, lists


def take_links(
    fields: list[FormField], values: dict[str, Any]
) -> dict[str, frozenset[str]]:
    """Remove the many-to-many values from ``values``: they are not columns."""
    taken: dict[str, frozenset[str]] = {}
    for field in fields:
        if field.kind == "links" and field.name in values:
            taken[field.name] = values.pop(field.name)
    return taken


async def save_row(
    site: Site,
    entry: ModelAdmin[Any],
    values: dict[str, Any],
    links: Mapping[str, frozenset[str]],
    row: Any,
) -> None:
    """Save the row, then replace the links inside the caller's scope. Run it
    in the form's savepoint: a refused link rolls the row back too."""
    saved = await entry.save(values, site.principal, row)
    key = getattr(saved, saved.__primary_key__)
    # The write locks the owner row, then re-checks it is still visible.
    owner = entry.queryset(site.principal).filter(**{saved.__primary_key__: key})
    for name, keys in links.items():
        side = side_of(entry, name)
        scope = scope_of(site, side)
        if scope is None:
            raise Refusal(LINK_REFUSAL)
        try:
            await getattr(saved, side.declared).write("set", sorted(keys), scope, owner)
        except ModelValidationError:
            raise Refusal(LINK_REFUSAL) from None
        except saved.NotFound:
            raise OutOfScope from None
