"""Startup-time binding of handler signatures to request data.

Current scope (R3): path parameters only. Every handler parameter must be a
path parameter of its template and have a supported annotation; ambiguous or
unsupported signatures fail at startup, never at request time.
"""

from __future__ import annotations

import inspect
import re
import typing
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from hinoki.errors import RegistrationError
from hinoki.routing import Route

_PATH_PARAM = re.compile(r"\{([^{}]+)\}")


class BadParameter(ValueError):
    """A path parameter does not match its declared type (-> 422)."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def _to_bool(raw: str) -> bool:
    if raw in {"true", "1"}:
        return True
    if raw in {"false", "0"}:
        return False
    raise ValueError(raw)


_CONVERTERS: dict[object, Callable[[str], object]] = {
    str: str,
    int: int,
    float: float,
    bool: _to_bool,
    UUID: UUID,
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


def bind(route: Route) -> Binding:
    in_path = set(_PATH_PARAM.findall(route.path))
    try:
        hints = typing.get_type_hints(route.handler)
        params = [
            p
            for p in inspect.signature(route.handler).parameters.values()
            if not (route.owner is not None and p.name == "self")
        ]
    except (NameError, TypeError, ValueError) as exc:
        raise RegistrationError(
            f"{route.name}: cannot inspect signature: {exc}"
        ) from exc
    declared = {p.name for p in params}
    if declared != in_path:
        extra, missing = declared - in_path, in_path - declared
        raise RegistrationError(
            f"{route.name}: parameters not in path {sorted(extra)}, "
            f"path parameters not accepted {sorted(missing)} "
            "(only path parameters are supported so far)"
        )
    converters: dict[str, Callable[[str], object]] = {}
    for param in params:
        convert = _CONVERTERS.get(hints.get(param.name))
        if convert is None:
            raise RegistrationError(
                f"{route.name}: unsupported type for path parameter {param.name!r}"
            )
        converters[param.name] = convert
    return Binding(route, converters)
