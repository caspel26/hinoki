"""``validate_response=True``: the handler's return value must match its annotation.

The check compiles once, when the action is bound, from the return annotation.
It is strict and never coerces: a ``dict`` is not a ``Schema``, ``True`` is not
an ``int``. A ``Schema`` instance is valid by construction, so the check is of
the shape: the declared class, ``list[...]`` of it, a ``Page`` of it, a scalar,
``None`` or a union of those. ``Response`` is the explicit escape hatch: an arm
``T | Response`` lets the handler return a ``Response`` that is not checked.
"""

from __future__ import annotations

import types
import typing
from collections.abc import Callable
from typing import Any

from hinoki.errors import RegistrationError
from hinoki.http.response import Response
from hinoki.pagination import Page
from hinoki.schema import Schema

Check = Callable[[object], bool]
SCALARS = (str, int, float, bool)


class ResponseContractError(RuntimeError):
    """The handler returned a value that its return annotation does not allow.

    A server fault, answered as ``500 internal_error``; the message names the
    action and the expected type, never the value."""


def compile_response_check(name: str, annotation: object) -> Check:
    """The check for ``annotation``; ``RegistrationError`` if it cannot be one."""
    if annotation is Any or annotation is object or annotation is None:
        raise RegistrationError(
            f"{name}: validate_response=True needs a concrete return annotation"
        )
    arms = union_arms(annotation)
    escape = Response in arms
    checks = [arm_check(name, arm) for arm in arms if arm is not Response]
    if not checks:
        raise RegistrationError(
            f"{name}: validate_response=True cannot check a route that only "
            "returns Response; annotate the value too, for example `Note | Response`"
        )
    if escape:
        checks.append(lambda value: isinstance(value, Response))
    return lambda value: any(check(value) for check in checks)


def union_arms(annotation: object) -> list[object]:
    """The arms of ``A | B`` (``Annotated`` unwrapped), or the annotation itself."""
    annotation = unwrap(annotation)
    if typing.get_origin(annotation) in {typing.Union, types.UnionType}:
        return [
            arm for member in typing.get_args(annotation) for arm in union_arms(member)
        ]
    return [annotation]


def unwrap(annotation: object) -> object:
    while typing.get_origin(annotation) is typing.Annotated:
        annotation = typing.get_args(annotation)[0]
    return annotation


def arm_check(name: str, arm: object) -> Check:
    """One non-union arm of the annotation as a check."""
    if arm is type(None):
        return lambda value: value is None
    if isinstance(arm, type) and issubclass(arm, Schema) and arm is not Schema:
        return lambda value: isinstance(value, arm)
    if arm in SCALARS:
        return lambda value: type(value) is arm
    return collection_check(name, arm)


def collection_check(name: str, arm: object) -> Check:
    """``list[X]`` or ``Page[X]`` of a checkable item type."""
    origin = typing.get_origin(arm)
    args = typing.get_args(arm)
    if origin not in {list, Page} or len(args) != 1:
        raise RegistrationError(
            f"{name}: validate_response=True cannot check the return type {arm!r}; "
            "use a Schema, list[Schema], Page[Schema], a scalar, None or a union"
        )
    item = compile_item(name, args[0])
    if origin is list:
        return lambda value: isinstance(value, list) and all(item(v) for v in value)
    return lambda value: isinstance(value, Page) and all(item(v) for v in value.items)


def compile_item(name: str, annotation: object) -> Check:
    """The check of a collection's item type (no ``Response`` escape inside)."""
    arms = union_arms(annotation)
    if Response in arms:
        raise RegistrationError(
            f"{name}: Response can only be returned whole, not inside a collection"
        )
    checks = [arm_check(name, arm) for arm in arms]
    return lambda value: any(check(value) for check in checks)
