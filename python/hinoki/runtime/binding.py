"""Binding of handler signatures to request data.

Rules, checked once when a route is included (never per request):

* a parameter named in the path template is a path parameter;
* a ``Schema`` parameter is the JSON body (one per action, POST/PUT/PATCH);
* any other supported scalar is a query parameter (optional if it has a default);
* anything else is ambiguous and rejected.

Handlers must be callable by keyword, and an ``API`` class must be
constructible without arguments.
"""

from __future__ import annotations

import inspect
import math
import re
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

from hinoki import native
from hinoki.auth import Principal
from hinoki.errors import (
    ErrorDetail,
    InvalidBody,
    RegistrationError,
    ValidationError,
)
from hinoki.pagination import PageNumberPagination, PageParams
from hinoki.request import Request
from hinoki.routing import Route
from hinoki.schema import Schema

PATH_PARAM = re.compile(r"\{([^{}]+)\}")
INTEGER = re.compile(r"-?[0-9]+")
DECIMAL = re.compile(r"-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?")
KEYWORD_KINDS = {
    inspect.Parameter.POSITIONAL_OR_KEYWORD,
    inspect.Parameter.KEYWORD_ONLY,
}
BODY_METHODS = {"POST", "PUT", "PATCH"}
EMPTY = inspect.Parameter.empty
MAX_FIELDS = 1000
MISSING = object()


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


KINDS: dict[object, str] = {
    str: "str",
    int: "int",
    float: "float",
    bool: "bool",
    UUID: "uuid",
}

CONVERTERS: dict[object, Callable[[str], object]] = {
    str: str,
    int: to_int,
    float: to_float,
    bool: to_bool,
    UUID: to_uuid,
}


def parse_query(raw: bytes) -> dict[str, list[str]]:
    """Parse a query string (in Rust); malformed input raises ``InvalidBody``."""
    try:
        return native.parse_query_string(raw, MAX_FIELDS)
    except ValueError:
        raise InvalidBody("invalid_query", "malformed query string") from None


@dataclass(slots=True)
class RequestData:
    """Everything a binding may read from a request."""

    path: dict[str, str] = field(default_factory=dict)
    query: dict[str, list[str]] = field(default_factory=dict)
    body: object = MISSING
    principal: object = MISSING


def problem(path: str, code: str, message: str) -> ValidationError:
    return ValidationError([ErrorDetail(path, code, message)])


@dataclass(frozen=True, slots=True)
class Param:
    """One handler parameter and where its value comes from."""

    name: str
    source: str  # "path", "query", "body", "principal" or "page"
    convert: Callable[[str], object] | None = None
    schema: type[Schema] | None = None
    default: object = EMPTY
    kind: str = "str"
    strategy: PageNumberPagination | None = None

    def extract(self, request: RequestData) -> object:
        """Value for this parameter, from the part of the request it belongs to."""
        return getattr(self, f"from_{self.source}")(request)

    def from_principal(self, request: RequestData) -> object:
        return request.principal

    def from_page(self, request: RequestData) -> object:
        if self.strategy is None:
            raise TypeError("page parameter without a pagination strategy")
        return self.strategy.parse(request.query)

    def from_path(self, request: RequestData) -> object:
        return self.convert_text(request.path[self.name])

    def convert_text(self, raw: str) -> object:
        assert self.convert is not None  # noqa: S101 - set for path/query params
        try:
            return self.convert(raw)
        except ValueError:
            raise problem(self.name, "type", "invalid value") from None

    def from_query(self, request: RequestData) -> object:
        values = request.query.get(self.name)
        if values is None:
            if self.default is EMPTY:
                raise problem(self.name, "required", "query parameter is required")
            return self.default
        if len(values) > 1:
            raise problem(self.name, "duplicate", "given more than once")
        return self.convert_text(values[0])

    def from_body(self, request: RequestData) -> object:
        assert self.schema is not None  # noqa: S101 - set for body params
        try:
            return self.schema.validate(request.body)
        except ValidationError as exc:
            prefixed = [
                ErrorDetail(
                    f"{self.name}.{d.path}" if d.path else self.name, d.code, d.message
                )
                for d in exc.details
            ]
            raise ValidationError(prefixed) from None


@dataclass(frozen=True, slots=True)
class Binding:
    route: Route
    params: tuple[Param, ...]

    @property
    def needs_body(self) -> bool:
        return any(p.source == "body" for p in self.params)

    def collect(self, request: RequestData) -> dict[str, object]:
        """Keyword arguments for the handler; raises ``ValidationError`` with
        every problem found."""
        values: dict[str, object] = {}
        errors: list[ErrorDetail] = []
        for param in self.params:
            try:
                values[param.name] = param.extract(request)
            except ValidationError as exc:
                errors.extend(exc.details)
        if errors:
            raise ValidationError(errors)
        return values


def handler_parameters(route: Route) -> list[inspect.Parameter]:
    """Every parameter after ``self``; the request is checked by ``bind``."""
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
                "keyword (no positional-only, *args or **kwargs)"
            )
    return parameters


def check_owner(route: Route) -> None:
    if route.owner is None:
        return
    required = [
        p.name
        for p in inspect.signature(route.owner).parameters.values()
        if p.default is EMPTY and p.kind in KEYWORD_KINDS
    ]
    if required:
        raise RegistrationError(
            f"{route.owner.__name__} is created without arguments per request; "
            f"its __init__ requires {required}"
        )


def is_principal(annotation: object) -> bool:
    return annotation is Principal or typing.get_origin(annotation) is Principal


def special_param(
    route: Route, parameter: inspect.Parameter, annotation: object
) -> Param | None:
    """Parameters that are not plain request values: page, principal, body."""
    name, where = parameter.name, route.name
    if annotation is PageParams:
        if route.pagination is None:
            raise RegistrationError(
                f"{where}: {name!r} asks for PageParams but the action has no "
                "pagination=... option"
            )
        return Param(name, "page", strategy=route.pagination)
    if is_principal(annotation):
        if route.access is None:
            raise RegistrationError(
                f"{where}: {name!r} asks for the principal but the action has no "
                "access policy"
            )
        return Param(name, "principal")
    if isinstance(annotation, type) and issubclass(annotation, Schema):
        if name in PATH_PARAM.findall(route.path) or route.method not in BODY_METHODS:
            raise RegistrationError(
                f"{where}: schema parameter {name!r} must be a JSON body of a "
                "POST/PUT/PATCH action, not a path or query parameter"
            )
        annotation.specs()  # unresolvable annotations fail now, not per request
        return Param(name, "body", schema=annotation)
    return None


def make_param(route: Route, parameter: inspect.Parameter, annotation: object) -> Param:
    special = special_param(route, parameter, annotation)
    if special is not None:
        return special
    name = parameter.name
    convert = CONVERTERS.get(annotation)
    if convert is None:
        raise RegistrationError(
            f"{route.name}: unsupported type for parameter {name!r}"
        )
    kind = KINDS[annotation]
    if name in PATH_PARAM.findall(route.path):
        return Param(name, "path", convert, kind=kind)
    return Param(name, "query", convert, default=parameter.default, kind=kind)


def split_request(
    route: Route, parameters: list[inspect.Parameter], hints: dict[str, typing.Any]
) -> list[inspect.Parameter]:
    """The request is the first parameter of every action and nothing else may
    be annotated with it; returns the remaining, bound parameters."""
    first = parameters[0] if parameters else None
    if (
        first is None
        or hints.get(first.name) is not Request
        or first.kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD
    ):
        raise RegistrationError(
            f"{route.name}: the first parameter must be the request, "
            "annotated `Request` (after `self` in an API class)"
        )
    return parameters[1:]


def bind(route: Route) -> Binding:
    parameters = handler_parameters(route)
    check_owner(route)
    try:
        hints = typing.get_type_hints(route.handler)
    except (NameError, TypeError) as exc:
        raise RegistrationError(
            f"{route.name}: cannot resolve annotations: {exc}"
        ) from exc
    parameters = split_request(route, parameters, hints)
    names = {p.name for p in parameters}
    missing = set(PATH_PARAM.findall(route.path)) - names
    if missing:
        raise RegistrationError(
            f"{route.name}: path parameters not accepted by the handler: "
            f"{sorted(missing)}"
        )
    params = tuple(make_param(route, p, hints.get(p.name)) for p in parameters)
    if route.pagination is not None and not any(p.source == "page" for p in params):
        raise RegistrationError(
            f"{route.name}: pagination=... needs a PageParams parameter"
        )
    if sum(p.source == "body" for p in params) > 1:
        raise RegistrationError(f"{route.name}: at most one body parameter")
    return Binding(route, params)
