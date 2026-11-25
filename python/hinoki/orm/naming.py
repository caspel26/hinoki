"""Deterministic defaults and physical-table collision rules for Models."""

from __future__ import annotations

from typing import ClassVar, Protocol

from hinoki import core
from hinoki.errors import RegistrationError

MIGRATION_LEDGER = "hinoki_migrations"


class _TableOwner(Protocol):
    __table__: ClassVar[str]
    __table_is_implicit__: ClassVar[bool]


def snake_case(name: str) -> str:
    """``PostTag`` becomes ``post_tag``: the words of a class name, lower case."""
    return core.snake_case(name)


def app_label(module: str) -> str:
    """The app a model module belongs to: the package that holds its ``models``
    module (``shop.apps.billing.models`` is ``billing``, ``shop.models.invoice``
    is ``shop``); without a ``models`` segment, the module's own last name."""
    return core.app_label(module)


def _validate_options(class_name: str, table: str, label: str, plural: str) -> None:
    for option, value in (("table", table), ("label", label), ("plural", plural)):
        if type(value) is not str:
            raise RegistrationError(f"{class_name}: {option}= must be a string")


def resolve_model_names(
    class_name: str,
    table: str,
    label: str,
    plural: str,
    *,
    abstract: bool,
    module: str = "",
    app: str = "",
) -> tuple[str, str, str, bool, str]:
    """Return resolved names, whether the table came from the class name and,
    for a derived table name that cannot be used, the deferred error. The
    table is ``<app>_<models>`` (the model name in the plural); ``app`` overrides
    the app taken from ``module``."""
    _validate_options(class_name, table, label, plural)
    try:
        return core.resolve_model_names(
            class_name, table, label, plural, abstract, module, app
        )
    except ValueError as exc:
        raise RegistrationError(str(exc)) from None


def table_names_conflict(left: type[_TableOwner], right: type[_TableOwner]) -> bool:
    """Whether portable table ownership makes two model classes ambiguous."""
    left_table = left.__table__
    right_table = right.__table__
    if left_table == right_table:
        return True
    return left_table.casefold() == right_table.casefold() and (
        left.__table_is_implicit__ or right.__table_is_implicit__
    )
