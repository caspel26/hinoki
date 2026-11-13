"""The per-model admin hooks: ``ModelAdmin`` and ``OutOfScope``."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from hinoki.admin.parts import Badge, Fieldset, Stat, Tab
from hinoki.auth import Principal
from hinoki.errors import RegistrationError
from hinoki.orm import Model, QuerySet


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

    def queryset(self, principal: Principal[Any]) -> QuerySet[M]:
        return self.model.all()

    def can(self, principal: Principal[Any], action: str, row: M | None = None) -> bool:
        return True

    def title_of(self, row: M) -> str:
        """A row's name: its first listed column when that is text."""
        first = getattr(row, self.shown_columns()[0], None)
        if isinstance(first, str) and first:
            return first
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
