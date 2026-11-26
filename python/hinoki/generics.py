"""Generic schemas: ``Envelope[User]`` is a concrete, cached ``Schema`` subclass."""

from __future__ import annotations

import inspect
import typing
from typing import Any, TypeVar

from hinoki.derive import recreate
from hinoki.errors import RegistrationError
from hinoki.schema import Schema, free_parameters

_CACHE: dict[tuple[type, tuple[Any, ...]], type] = {}
_BUILDING: set[tuple[type, tuple[Any, ...]]] = set()


TYPE_ARGUMENTS = "__type_arguments__"
"""The class attribute that maps the type parameters of a parameterized class
(``Envelope[User]``) to its arguments."""


def substitute(value: Any, mapping: dict[Any, Any]) -> Any:
    if isinstance(value, TypeVar):
        return mapping.get(value, value)
    params = free_parameters(value)
    if not params:
        return value
    return value[tuple(mapping.get(p, p) for p in params)]


def argument_name(value: Any) -> str:
    name = getattr(value, "__name__", None)
    return name if isinstance(name, str) and not free_parameters(value) else repr(value)


def parameterize(cls: type[Schema], args: tuple[Any, ...]) -> type[Schema]:
    params = cls.__parameters__  # type: ignore[attr-defined]
    if len(args) != len(params):
        raise TypeError(
            f"{cls.__name__} takes {len(params)} type argument(s), got {len(args)}"
        )
    key = (cls, args)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    if key in _BUILDING:
        raise RegistrationError(
            f"{cls.__name__}: recursive generic schemas are not supported"
        )
    _BUILDING.add(key)
    try:
        built = build(cls, dict(zip(params, args, strict=True)), args)
    finally:
        _BUILDING.discard(key)
    _CACHE[key] = built
    return built


def build(cls: type[Schema], mapping: dict[Any, Any], args: tuple[Any, ...]) -> type:
    local = {p.__name__: p for p in mapping}
    try:
        hints = typing.get_type_hints(cls, localns=local, include_extras=True)
    except NameError as exc:
        raise RegistrationError(f"{cls.__name__}: {exc}") from exc
    own = inspect.get_annotations(cls)
    name = f"{cls.__name__}[{', '.join(argument_name(a) for a in args)}]"
    body = {
        **cls.__source__,
        "__annotations__": {n: substitute(hints[n], mapping) for n in own},
        "__qualname__": name,
    }
    bases = tuple(b for b in cls.__bases__ if b is not typing.Generic)
    built = recreate(cls, body, bases, name)
    setattr(built, TYPE_ARGUMENTS, mapping)
    return built
