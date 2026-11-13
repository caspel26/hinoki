"""Startup-time binding of handler signatures to request data.

Current scope: path parameters only. Every handler parameter must be a plain
(keyword-capable) path parameter of its template with a supported annotation,
and an ``API`` class must be constructible without arguments. Anything else
fails at startup, never at request time.
"""

from __future__ import annotations

import inspect
import math
import re
import typing
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from hinoki.errors import RegistrationError
from hinoki.routing import Route

PATH_PARAM = re.compile(r"\{([^{}]+)\}")
INTEGER = re.compile(r"-?[0-9]+")
DECIMAL = re.compile(r"-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?")
KEYWORD_KINDS = {
    inspect.Parameter.POSITIONAL_OR_KEYWORD,
    inspect.Parameter.KEYWORD_ONLY,
}


class BadParameter(ValueError):
    """A path parameter does not match its declared type (-> 422)."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def to_bool(raw: str) -> bool:
    if raw in {"true", "1"}:
        return True
    if raw in {"false", "0"}:
        return False
    raise ValueError(raw)


def to_int(raw: str) -> int:
    if not INTEGER.fullmatch(raw):
        raise ValueError(raw)
    return int(raw)


def to_float(raw: str) -> float:
    value = float(raw) if DECIMAL.fullmatch(raw) else math.nan
    if not math.isfinite(value):
        raise ValueError(raw)
    return value


def to_uuid(raw: str) -> UUID:
    value = UUID(raw)
    if str(value) != raw.lower():
        raise ValueError(raw)  # only the canonical 8-4-4-4-12 form
    return value


CONVERTERS: dict[object, Callable[[str], object]] = {
    str: str,
    int: to_int,
    float: to_float,
    bool: to_bool,
    UUID: to_uuid,
}


@dataclass(frozen=True, slots=True)
class Binding:
    route: Route
    converters: dict[str, Callable[[str], object]]

    def convert(self, raw: dict[str, str]) -> dict[str, object]:
        out: dict[str, object] = {}
        for name, convert in self.converters.items():
            try:
                out[name] = convert(raw[name])
            except (ValueError, KeyError):
                raise BadParameter(name) from None
        return out


def handler_parameters(route: Route) -> list[inspect.Parameter]:
    try:
        parameters = list(inspect.signature(route.handler).parameters.values())
    except (TypeError, ValueError) as exc:
        raise RegistrationError(
            f"{route.name}: cannot inspect signature: {exc}"
        ) from exc
    if route.owner is not None:
        parameters = parameters[1:]  # self
    for parameter in parameters:
        if parameter.kind not in KEYWORD_KINDS:
            raise RegistrationError(
                f"{route.name}: parameter {parameter.name!r} must be usable by "
                "keyword "
                "(no positional-only, *args or **kwargs)"
            )
    return parameters


def check_owner(route: Route) -> None:
    if route.owner is None:
        return
    required = [
        p.name
        for p in inspect.signature(route.owner).parameters.values()
        if p.default is inspect.Parameter.empty and p.kind in KEYWORD_KINDS
    ]
    if required:
        raise RegistrationError(
            f"{route.owner.__name__} is created without arguments per request; "
            f"its __init__ requires {required}"
        )


def bind(route: Route) -> Binding:
    parameters = handler_parameters(route)
    check_owner(route)
    in_path = set(PATH_PARAM.findall(route.path))
    declared = {p.name for p in parameters}
    if declared != in_path:
        raise RegistrationError(
            f"{route.name}: parameters not in path {sorted(declared - in_path)}, "
            f"path parameters not accepted {sorted(in_path - declared)} "
            "(only path parameters are supported so far)"
        )
    try:
        hints = typing.get_type_hints(route.handler)
    except (NameError, TypeError) as exc:
        raise RegistrationError(
            f"{route.name}: cannot resolve annotations: {exc}"
        ) from exc
    converters: dict[str, Callable[[str], object]] = {}
    for parameter in parameters:
        convert = CONVERTERS.get(hints.get(parameter.name))
        if convert is None:
            raise RegistrationError(
                f"{route.name}: unsupported type for path parameter {parameter.name!r}"
            )
        converters[parameter.name] = convert
    return Binding(route, converters)
