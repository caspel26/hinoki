"""The per-model admin hooks: ``ModelAdmin`` and ``OutOfScope``."""

from __future__ import annotations

import inspect
import re
import typing
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from hinoki.admin.parts import Badge, Extra, Fieldset, Stat, Tab
from hinoki.auth import Caller
from hinoki.errors import RegistrationError
from hinoki.orm import Model, QuerySet

_ACTION_NAME = re.compile(r"[a-z][a-z0-9-]{0,47}\Z")
# The names the permission model uses; a bulk action cannot take one.
RESERVED_ACTIONS = frozenset({"view", "add", "change", "delete"})
_MAX_ACTION_LABEL = 80
_MAX_ACTION_CONFIRMATION = 240


@dataclass(frozen=True, slots=True)
class BulkAction[M: Model]:
    """A confirmed action for selected rows in a model's admin list."""

    name: str
    label: str
    confirm: str
    handler: Callable[[tuple[M, ...], Caller[Any]], Awaitable[None]]


ACTION_MARK = "__admin_action__"


def action[F: Callable[..., Any]](
    label: str = "", *, confirm: str = ""
) -> Callable[[F], F]:
    """Declare a bulk action on a ``ModelAdmin`` method.

        @action(confirm="Publish the selected posts?")
        async def publish(self, rows, principal): ...

    The name is the function's (``_`` becomes ``-``) and the permission checked
    by ``ModelAdmin.can``; the label defaults to "Publish selected".
    """

    def mark(function: F) -> F:
        setattr(function, ACTION_MARK, (label, confirm))
        return function

    return mark


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
    slug: ClassVar[str] = ""
    label: ClassVar[str] = ""
    icon: ClassVar[str] = "file-text"
    columns: ClassVar[tuple[str, ...]] = ()
    search: ClassVar[tuple[str, ...]] = ()
    ordering: ClassVar[tuple[str, ...]] = ()
    exclude: ClassVar[tuple[str, ...]] = ()
    readonly: ClassVar[tuple[str, ...]] = ()
    assign: ClassVar[Mapping[str, Callable[[Any], Any]]] = {}
    badges: ClassVar[Mapping[str, Mapping[Any, Badge | tuple[Any, ...]]]] = {}
    tabs: ClassVar[tuple[Tab | str, ...]] = ()
    fieldsets: ClassVar[tuple[Fieldset, ...] | Mapping[str, tuple[str, ...]]] = ()
    modified_field: ClassVar[str] = ""
    modified_label: ClassVar[str] = "Last modified"
    per_page: ClassVar[int | None] = None  # the site's ``per_page`` when unset
    show_count: ClassVar[bool | None] = None  # the site's ``show_count`` when unset
    bulk_delete: ClassVar[bool] = True  # offered where ``can(principal, "delete")``
    bulk_actions: ClassVar[tuple[BulkAction[Any], ...]] = ()
    extra_fields: ClassVar[tuple[Extra, ...]] = ()
    site: Any = None  # the Admin this model admin is registered in

    def __init_subclass__(cls, model: type[M] | None = None, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        generic = generic_model(cls)
        if model is not None and generic is not None and model is not generic:
            raise RegistrationError(
                f"{cls.__name__}: model={model.__name__} contradicts "
                f"ModelAdmin[{generic.__name__}]"
            )
        chosen = model or generic
        if chosen is not None:
            cls.model = chosen  # type: ignore[assignment]

    def __init__(self) -> None:
        declared = tuple(
            BulkAction(
                name.replace("_", "-"),
                label or f"{name.replace('_', ' ').capitalize()} selected",
                confirm or f"{name.replace('_', ' ').capitalize()} the selected rows?",
                getattr(self, name),
            )
            for name, (label, confirm) in declared_actions(type(self)).items()
        )
        declared_here = type(self).bulk_actions
        if declared or isinstance(declared_here, tuple):
            self.bulk_actions = (*declared_here, *declared)  # type: ignore[misc]
        self.tab_list: tuple[Tab, ...] = ()  # set by ``normalize`` at registration
        self.fieldset_list: tuple[Fieldset, ...] = ()

    def extras(self) -> tuple[Extra, ...]:
        """The form fields that are not columns (``extra_fields`` by default)."""
        return self.extra_fields

    async def extra_initial(self, row: M) -> dict[str, Any]:
        """What the extra fields show for ``row`` on the edit form."""
        return {}

    def queryset(self, principal: Caller[Any]) -> QuerySet[M]:
        return self.model.all()

    def can(self, principal: Caller[Any], action: str, row: M | None = None) -> bool:
        return True

    def title_of(self, row: M) -> str:
        """A row's name: the first listed column that holds text."""
        for column in self.shown_columns():
            value = getattr(row, column, None)
            if isinstance(value, str) and value:
                return value
        return f"{type(row).__name__} {getattr(row, self.model.__primary_key__)}"

    def display(self, row: M, column: str) -> Any:
        """The value shown in a list column (return a ``Badge`` or ``Avatar`` for
        richer cells)."""
        value = getattr(row, column)
        mapping = self.badges.get(column)
        if mapping is not None and value in mapping:
            return mapping[value]
        return value

    async def stats(self, principal: Caller[Any]) -> list[Stat]:
        total = await self.queryset(principal).acount()
        return [Stat(self.plural(), total, self.icon)]

    async def check(
        self, values: dict[str, Any], principal: Caller[Any], row: M | None
    ) -> dict[str, str]:
        """Cross-field validation hook: return ``{field: message}`` to refuse."""
        return {}

    async def save(
        self, values: dict[str, Any], principal: Caller[Any], row: M | None = None
    ) -> M:
        key = self.model.__primary_key__
        if row is None:
            made = {**values, **{c: f(principal) for c, f in self.assign.items()}}
            created = await self.model.acreate(**made)
            await self.ensure_visible(created, principal)
            return created
        if not values:
            return row
        if row.has_save_effects():
            # The row was loaded through this admin's queryset: save the instance
            # so its hooks and events run, then check it is still visible.
            saved = await row.aupdate_from(**values)
            await self.ensure_visible(saved, principal)
            return saved
        found = (
            await self.queryset(principal)
            .filter(**{key: getattr(row, key)})
            .aupdate(values)
        )
        if not found:
            raise self.model.NotFound(f"{self.model.__name__} not found")
        await self.ensure_visible(found[0], principal)
        return found[0]

    async def remove(self, row: M, principal: Caller[Any]) -> None:
        key = self.model.__primary_key__
        gone = (
            await self.queryset(principal).filter(**{key: getattr(row, key)}).adelete()
        )
        if not gone:
            raise self.model.NotFound(f"{self.model.__name__} not found")

    async def ensure_visible(self, row: M, principal: Caller[Any]) -> None:
        key = self.model.__primary_key__
        query = self.queryset(principal).filter(**{key: getattr(row, key)})
        if not await query.aexists():
            raise OutOfScope

    def plural(self) -> str:
        return self.label or self.model.__plural__ or f"{self.model.__name__}s"

    def shown_columns(self) -> tuple[str, ...]:
        """The list columns: ``columns``, else the first six that are not
        ``exclude``d (an excluded column is never displayed or sortable)."""
        if self.columns:
            return tuple(c for c in self.columns if c not in self.exclude)
        visible = [c for c in self.model.__columns__ if c not in self.exclude]
        return tuple(visible[:6])

    def default_search(self) -> tuple[str, ...]:
        """The first three shown text columns: what a search box is expected to
        look in, without ever reaching a column the list does not show."""
        kinds = self.model.__field_types__
        return tuple(
            name
            for name in self.shown_columns()
            if name in kinds and kinds[name].kind == "str"
        )[:3]

    def skipped(self) -> frozenset[str]:
        return frozenset(self.exclude) | frozenset(self.assign)

    def normalize(self, slug: str) -> None:
        """Fill the defaults the model implies and expand the short forms of
        ``badges``, ``tabs`` and ``fieldsets``."""
        if not self.search:
            self.search = self.default_search()  # type: ignore[misc]
        if not self.modified_field and self.model.__updated_at__:
            self.modified_field = self.model.__updated_at__  # type: ignore[misc]
        badges = {
            column: {
                value: item if isinstance(item, Badge) else Badge(*item)
                for value, item in mapping.items()
            }
            for column, mapping in self.badges.items()
        }
        self.badges = badges  # type: ignore[misc]
        tabs: list[Tab] = []
        for item in self.tabs:
            tabs.extend(
                tabs_from(slug, item, badges) if isinstance(item, str) else [item]
            )
        self.tab_list = tuple(tabs)
        declared = self.fieldsets
        self.fieldset_list = (
            tuple(Fieldset(title, tuple(fields)) for title, fields in declared.items())
            if isinstance(declared, Mapping)
            else tuple(declared)
        )

    def validate_config(self, slug: str) -> None:
        self.normalize(slug)
        names = set(self.model.__columns__)
        link_ids = {
            side.ids_name
            for side in getattr(self.model, "__links__", {}).values()
            if side.forward
        }
        for label, used in (
            ("columns", self.columns),
            ("search", self.search),
            ("ordering", [o.lstrip("-") for o in self.ordering]),
            ("exclude", [n for n in self.exclude if n not in link_ids]),
            ("readonly", [n for n in self.readonly if n not in link_ids]),
            ("assign", self.assign),
            ("badges", self.badges),
            ("modified_field", [self.modified_field] if self.modified_field else []),
        ):
            unknown = sorted(set(used) - names)
            if unknown:
                raise RegistrationError(
                    f"admin {slug!r}: {label} names unknown columns {unknown}"
                )
        hidden = sorted(set(self.search) & set(self.exclude))
        if hidden:
            raise RegistrationError(
                f"admin {slug!r}: search names excluded columns {hidden}"
            )
        self._validate_bulk_actions(slug)
        for tab in self.tab_list:
            for column in tab.filters:
                self.model.check_field(column.partition("__")[0])
        for fieldset in self.fieldset_list:
            unknown = sorted(set(fieldset.fields) - names - link_ids)
            if unknown:
                raise RegistrationError(
                    f"admin {slug!r}: fieldset {fieldset.title!r} names {unknown}"
                )

    def _validate_bulk_actions(self, slug: str) -> None:
        if not isinstance(self.bulk_actions, tuple):
            raise RegistrationError(f"admin {slug!r}: bulk_actions must be a tuple")
        action_names: set[str] = set()
        for action in self.bulk_actions:
            if not isinstance(action, BulkAction):
                raise RegistrationError(
                    f"admin {slug!r}: bulk_actions must contain BulkAction values"
                )
            _validate_bulk_action(action, slug, action_names)


def generic_model(cls: type) -> type[Model] | None:
    """The model a class names as ``ModelAdmin[Model]``, if it does."""
    for base in getattr(cls, "__orig_bases__", ()):
        arguments = typing.get_args(base)
        if typing.get_origin(base) is ModelAdmin and arguments:
            found = arguments[0]
            if isinstance(found, type) and issubclass(found, Model):
                return found
    return None


def declared_actions(cls: type) -> dict[str, tuple[str, str]]:
    """The methods marked with ``@action``, in definition order, base classes first."""
    found: dict[str, tuple[str, str]] = {}
    for klass in reversed(cls.__mro__):
        for name, value in vars(klass).items():
            if hasattr(value, ACTION_MARK):
                found[name] = getattr(value, ACTION_MARK)
    return found


def tabs_from(
    slug: str, column: str, badges: Mapping[str, Mapping[Any, Badge]]
) -> list[Tab]:
    """One tab for each value that ``badges`` names for ``column``."""
    mapping = badges.get(column)
    if not mapping:
        raise RegistrationError(
            f"admin {slug!r}: tabs names {column!r}, which has no entry in badges"
        )
    return [Tab(badge.label, {column: value}) for value, badge in mapping.items()]


def _validate_bulk_action(action: BulkAction[Any], slug: str, names: set[str]) -> None:
    if (
        not isinstance(action.name, str)
        or not _ACTION_NAME.fullmatch(action.name)
        or action.name in RESERVED_ACTIONS
    ):
        raise RegistrationError(
            f"admin {slug!r}: invalid or reserved bulk action name {action.name!r}"
        )
    if action.name in names:
        raise RegistrationError(
            f"admin {slug!r}: duplicate bulk action {action.name!r}"
        )
    names.add(action.name)
    if (
        not isinstance(action.label, str)
        or not action.label.strip()
        or len(action.label) > _MAX_ACTION_LABEL
    ):
        raise RegistrationError(
            f"admin {slug!r}: bulk action {action.name!r} label must contain "
            f"1 to {_MAX_ACTION_LABEL} characters"
        )
    if (
        not isinstance(action.confirm, str)
        or not action.confirm.strip()
        or len(action.confirm) > _MAX_ACTION_CONFIRMATION
    ):
        raise RegistrationError(
            f"admin {slug!r}: bulk action {action.name!r} confirmation must "
            f"contain 1 to {_MAX_ACTION_CONFIRMATION} characters"
        )
    if not callable(action.handler):
        raise RegistrationError(
            f"admin {slug!r}: bulk action {action.name!r} handler must be "
            "an async callable"
        )
    async_call = inspect.iscoroutinefunction(
        getattr(type(action.handler), "__call__", None)  # noqa: B004 - detect async callable instances
    )
    if not inspect.iscoroutinefunction(action.handler) and not async_call:
        raise RegistrationError(
            f"admin {slug!r}: bulk action {action.name!r} handler must be "
            "an async callable"
        )
