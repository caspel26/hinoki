"""``validate_response=True``: the handler's return value must match its annotation.

The check compiles once, when the action is bound, from the return annotation.
It is strict and never coerces: a ``dict`` is not a ``Schema``, ``True`` is not
an ``int``. A ``Schema`` instance is valid by construction, so the check is of
the shape: the declared class, ``list[...]`` of it, a ``Page`` of it, a scalar,
``None`` or a union of those. ``Response`` is the explicit escape hatch: an arm
``T | Response`` lets the handler return a ``Response`` that is not checked.
"""

from __future__ import annotations

import datetime
import types
import typing
from collections.abc import Callable
from decimal import Decimal
from typing import Any
from uuid import UUID

from hinoki import core
from hinoki.errors import RegistrationError
from hinoki.http.response import Response
from hinoki.orm.model import Model
from hinoki.orm.projection import _ProjectedList, _ProjectedPage
from hinoki.pagination import CursorPage, Page
from hinoki.schema import Schema

Check = Callable[[object], bool]
SCALARS = (
    str,
    int,
    float,
    bool,
    bytes,
    datetime.datetime,
    datetime.date,
    UUID,
    Decimal,
)
PAIR = 2
NO_ANNOTATION = object()  # the action has no return annotation


class ResponseContractError(RuntimeError):
    """The handler returned a value that its return annotation does not allow.

    A server fault, answered as ``500 internal_error``; the message names the
    action and the expected type, never the value."""


def compile_response_check(name: str, annotation: object) -> Check:
    """The check for ``annotation``; ``RegistrationError`` if it cannot be one.

    ``annotation`` is ``NO_ANNOTATION`` when the action has no return annotation."""
    if annotation is NO_ANNOTATION or annotation is Any or annotation is object:
        raise RegistrationError(
            f"{name}: the return annotation must name a concrete type so the "
            "response can be checked (a Schema, list[Schema], Page[Schema], a "
            "scalar, dict, None or a union); annotate it, or declare the action "
            "with validate_response=False"
        )
    try:
        arms = union_arms(annotation)
        checks = [arm_check(name, arm) for arm in arms if arm is not Response]
    except RecursionError:  # a type alias that refers to itself
        raise RegistrationError(
            f"{name}: a recursive return type cannot be checked; declare the "
            "action with validate_response=False"
        ) from None
    if Response in arms:
        checks.append(lambda value: isinstance(value, Response))
    return any_arm(checks)


def any_arm(checks: list[Check]) -> Check:
    """The check that accepts what any of ``checks`` accepts; a single one is
    used as it is, without a wrapper call per response."""
    if len(checks) == 1:
        return checks[0]
    arms = tuple(checks)

    def any_check(value: object) -> bool:
        for check in arms:  # noqa: SIM110 - a plain loop avoids a generator per response
            if check(value):
                return True
        return False

    return any_check


def union_arms(annotation: object) -> list[object]:
    """The arms of ``A | B`` (``Annotated`` unwrapped), or the annotation itself."""
    annotation = unwrap(annotation)
    if typing.get_origin(annotation) in {typing.Union, types.UnionType}:
        return [
            arm for member in typing.get_args(annotation) for arm in union_arms(member)
        ]
    return [annotation]


def unwrap(annotation: object) -> object:
    """``annotation`` without ``Annotated`` and without a PEP 695 ``type`` alias
    (``type Items = list[Item]`` is ``list[Item]``, ``Box[Item]`` of
    ``type Box[T] = list[T]`` too)."""
    while True:
        origin = typing.get_origin(annotation)
        if origin is typing.Annotated:
            annotation = typing.get_args(annotation)[0]
        elif isinstance(annotation, typing.TypeAliasType):
            annotation = annotation.__value__
        elif isinstance(origin, typing.TypeAliasType):
            annotation = origin.__value__[typing.get_args(annotation)]
        else:
            return annotation


def arm_check(name: str, arm: object) -> Check:
    """One non-union arm of the annotation as a check."""
    simple = simple_check(arm)
    return simple if simple is not None else collection_check(name, arm)


def simple_check(arm: object) -> Check | None:
    """The check of a plain class, ``None`` or an accept-anything arm."""
    if arm is Any or arm is object:
        return lambda _value: True
    if arm is type(None):
        return lambda value: value is None
    return class_check(arm) if isinstance(arm, type) else None


def class_check(arm: type) -> Check | None:
    if arm in SCALARS:
        return lambda value: type(value) is arm  # exact: True is not an int
    if arm in {dict, list} or issubclass(arm, (Response, Model, Schema)):
        return lambda value: isinstance(value, arm)
    return None


def collection_check(name: str, arm: object) -> Check:
    """``list[X]``, ``dict[str, X]`` or ``Page[X]`` of a checkable item type."""
    origin = typing.get_origin(arm)
    args = typing.get_args(arm)
    if origin is dict and len(args) == PAIR and args[0] is str:
        values = compile_item(name, args[1])
        return lambda value: (
            isinstance(value, dict)
            and all(type(key) is str and values(item) for key, item in value.items())
        )
    if origin not in {list, Page, CursorPage} or len(args) != 1:
        raise RegistrationError(
            f"{name}: the return type {arm!r} cannot be checked; use a Schema, "
            "Model, list, Page, CursorPage, dict[str, ...], a scalar, None or a "
            "union, or declare the action with validate_response=False"
        )
    item = compile_item(name, args[0])
    if origin is list:
        return list_check(args[0], item)
    if origin is Page:
        return page_check(args[0], item)
    container = origin
    return lambda value: (
        isinstance(value, container) and all(item(v) for v in value.items)
    )


def _schema_matches_annotation(schema_cls: type[Schema], annotation: object) -> bool:
    for arm in union_arms(annotation):
        nested = unwrap(arm)
        if nested is typing.Any or nested is object:
            return True
        if isinstance(nested, type) and issubclass(schema_cls, nested):
            return True
    return False


class PageCheck:
    """The check of ``Page[X]``. ``scan`` is the check and the narrowing scan in
    one pass: 0 invalid, 1 valid with every item exactly ``X``, 2 valid otherwise."""

    def __init__(self, annotation: object, item: Check) -> None:
        self.annotation = annotation
        self.item = item
        nested = unwrap(annotation)
        self.native: core._ResponseTypeList | None = (
            core._ResponseTypeList(nested, self.items_fallback)
            if isinstance(nested, type) and issubclass(nested, Schema | Model)
            else None
        )

    def items_fallback(self, value: object) -> bool:
        return isinstance(value, list) and all(self.item(v) for v in value)

    def scan(self, value: object) -> int:
        if not isinstance(value, Page):
            return 0
        if isinstance(value, _ProjectedPage) and value._projected_handle is not None:
            return (
                2 if _schema_matches_annotation(value._schema, self.annotation) else 0
            )
        if self.native is not None:
            return self.native.scan(value.items)
        return 2 if self.items_fallback(value.items) else 0

    def __call__(self, value: object) -> bool:
        return self.scan(value) != 0


def page_check(annotation: object, item: Check) -> Check:
    return PageCheck(annotation, item)


def list_check(annotation: object, item: Check) -> Check:
    def fallback(value: object) -> bool:
        if not isinstance(value, list):
            return False
        if isinstance(value, _ProjectedList) and value._projected_handle is not None:
            return _schema_matches_annotation(value._schema, annotation)
        return all(item(v) for v in value)

    nested = unwrap(annotation)
    if typing.get_origin(nested) is dict and typing.get_args(nested) == (str, object):
        return core._ResponseObjectList(fallback, item)
    if isinstance(nested, type) and issubclass(nested, Schema | Model):
        return core._ResponseTypeList(nested, fallback)
    return fallback


def compile_item(name: str, annotation: object) -> Check:
    """The check of a collection's item type (no ``Response`` escape inside)."""
    arms = union_arms(annotation)
    if Response in arms:
        raise RegistrationError(
            f"{name}: Response can only be returned whole, not inside a collection; "
            "declare the action with validate_response=False to skip the check"
        )
    checks = [arm_check(name, arm) for arm in arms]
    return any_arm(checks)
