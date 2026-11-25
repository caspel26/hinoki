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
import re
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from types import NoneType, UnionType
from uuid import UUID

from hinoki import core
from hinoki.auth import Caller, session_auth
from hinoki.errors import (
    ErrorDetail,
    InvalidBody,
    RegistrationError,
    ValidationError,
)
from hinoki.fields import carries_field
from hinoki.filters import FilterSet
from hinoki.handlerhints import hints_of, signature_of
from hinoki.http.request import Request
from hinoki.http.response import Response
from hinoki.http.stream import Streamed
from hinoki.modules import Resource
from hinoki.pagination import (
    CursorPage,
    CursorPagination,
    CursorParams,
    Page,
    PageNumberPagination,
    PageParams,
    Pagination,
)
from hinoki.parsers import MultiPartParser, Parser, UploadedFile, XMLParser
from hinoki.renderers import is_default_json_renderer
from hinoki.routing import Route
from hinoki.runtime.deprecation import header_of
from hinoki.runtime.responsecheck import (
    NO_ANNOTATION,
    Check,
    compile_response_check,
    union_arms,
    unwrap,
)
from hinoki.runtime.responses import BODYLESS
from hinoki.schema import Schema
from hinoki.sources import TextSource

if typing.TYPE_CHECKING:
    from hinoki.runtime.rowplan import RowWireSelection

PATH_PARAM = re.compile(r"\{([^{}]+)\}")
INTEGER = re.compile(r"-?[0-9]+")
DECIMAL = re.compile(r"-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?")
KEYWORD_KINDS = {
    inspect.Parameter.POSITIONAL_OR_KEYWORD,
    inspect.Parameter.KEYWORD_ONLY,
}
BODY_METHODS = {"POST", "PUT", "PATCH", "QUERY"}
EMPTY = inspect.Parameter.empty
MAX_FIELDS = 1000
MISSING = object()
OPTIONAL_UNION_ARITY = 2


def to_bool(raw: str) -> bool:
    return core.parse_bool(raw)


def to_int(raw: str) -> int:
    return core.parse_int(raw)


def to_float(raw: str) -> float:
    return core.parse_float(raw)


def to_uuid(raw: str) -> UUID:
    return core.parse_uuid(raw)


KINDS: dict[object, str] = {
    str: "str",
    int: "int",
    float: "float",
    bool: "bool",
    UUID: "uuid",
}

KINDS_BY_NAME = {kind: annotation for annotation, kind in KINDS.items()}

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
        return core.parse_query_string(raw, MAX_FIELDS)
    except ValueError:
        raise InvalidBody("invalid_query", "malformed query string") from None


@dataclass(slots=True)
class RequestData:
    """Everything a binding may read from a request."""

    path: dict[str, str] = field(default_factory=dict)
    query: dict[str, list[str]] = field(default_factory=dict)
    body: object = MISSING
    principal: object = MISSING
    files: dict[str, list[UploadedFile]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TypedBody:
    """A body parameter Rust already read and validated from the bytes."""

    value: object


def problem(path: str, code: str, message: str) -> ValidationError:
    return ValidationError([ErrorDetail(path, code, message)])


@dataclass(frozen=True, slots=True)
class Param:
    """One handler parameter and where its value comes from."""

    name: str
    source: str  # request data, principal, resource or pagination
    convert: Callable[[str], object] | None = None
    schema: type[Schema] | None = None
    default: object = EMPTY
    kind: str = "str"
    strategy: Pagination | None = None
    file_mode: str = ""
    text: bool = False  # a Schema read from a text source, converted before checking
    filters: type[FilterSet] | None = None
    resource_type: type[Resource] | None = None

    def extract(self, request: RequestData) -> object:
        """Value for this parameter, from the part of the request it belongs to."""
        return getattr(self, f"from_{self.source}")(request)

    def from_principal(self, request: RequestData) -> object:
        return request.principal

    def from_filters(self, request: RequestData) -> object:
        if self.filters is None:
            raise TypeError("filters parameter without a FilterSet")
        reserved = self.strategy.names if self.strategy is not None else ()
        return self.filters.parse(request.query, reserved)

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
        if type(request.body) is TypedBody:
            return request.body.value
        return self.checked(request.body)

    def from_queryschema(self, request: RequestData) -> object:
        return self.checked(request.query)

    def checked(self, data: object) -> object:
        assert self.schema is not None  # noqa: S101 - set for Schema params
        try:
            if self.text:
                return self.schema.validate_text(data)
            return self.schema.validate(data)
        except ValidationError as exc:
            prefixed = [
                ErrorDetail(
                    f"{self.name}.{d.path}" if d.path else self.name, d.code, d.message
                )
                for d in exc.details
            ]
            raise ValidationError(prefixed) from None

    def from_file(self, request: RequestData) -> object:
        """Return the uploaded file value or a field-specific validation error."""
        files = request.files.get(self.name, [])
        if self.file_mode == "many":
            if not files:
                raise problem(self.name, "required", "at least one file is required")
            return files
        if not files and self.file_mode == "optional":
            return None
        if not files:
            raise problem(self.name, "required", "file is required")
        if len(files) > 1:
            raise problem(self.name, "duplicate", "given more than once")
        return files[0]


@dataclass(frozen=True, slots=True)
class Binding:
    route: Route
    params: tuple[Param, ...]
    takes_request: bool = True  # False: the action did not declare `Request`
    needs_body: bool = False  # a parameter binds the body; decided once in `bind`
    response_schema: type[Schema] | None = None
    response_check: Check | None = field(default=None, repr=False, compare=False)
    response_annotation: str = field(default="", repr=False, compare=False)
    row_wire: RowWireSelection | None = field(default=None, repr=False, compare=False)
    streams: bool = False  # the return annotation names Stream, EventStream or File
    # S when the return type is `list[S]`, `Page[S]`, `CursorPage[S]` or `S | None`
    narrow_schema: type[Schema] | None = None
    stream_capable: bool = False  # Response may be a stream, including FileStorage
    is_async: bool = field(init=False, repr=False, compare=False)
    native_input_names: tuple[str, ...] = field(init=False, repr=False, compare=False)
    body_param: Param | None = field(init=False, repr=False, compare=False)
    principal_name: str | None = field(init=False, repr=False, compare=False)
    default_json: bool = field(init=False, repr=False, compare=False)
    fused_check: bool = field(init=False, repr=False, compare=False)
    native_defaults: tuple[tuple[str, object], ...] = field(
        init=False, repr=False, compare=False
    )
    has_files: bool = field(init=False, repr=False, compare=False)
    takes_text: bool = field(init=False, repr=False, compare=False)
    needs_request: bool = field(init=False, repr=False, compare=False)
    resource_params: tuple[Param, ...] = field(init=False, repr=False, compare=False)
    session_params: tuple[Param, ...] = field(init=False, repr=False, compare=False)
    needs_session: bool = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Freeze handler execution shape with the request binding plan."""
        object.__setattr__(
            self, "is_async", inspect.iscoroutinefunction(self.route.handler)
        )
        object.__setattr__(
            self,
            "native_input_names",
            tuple(
                p.name for p in self.params if p.source not in {"resource", "session"}
            ),
        )
        renderers = self.route.renderers
        object.__setattr__(
            self,
            "default_json",
            len(renderers) == 1 and is_default_json_renderer(renderers[0]),
        )
        object.__setattr__(
            self,
            "fused_check",
            self.default_json
            and self.response_schema is None
            and isinstance(self.response_check, core._ResponseObjectList)
            and self.route.status not in BODYLESS,
        )
        principals = [p.name for p in self.params if p.source == "principal"]
        object.__setattr__(
            self, "principal_name", principals[0] if len(principals) == 1 else None
        )
        has_files = any(p.source == "file" for p in self.params)
        object.__setattr__(self, "has_files", has_files)
        object.__setattr__(
            self,
            "resource_params",
            tuple(p for p in self.params if p.source == "resource"),
        )
        object.__setattr__(
            self,
            "session_params",
            tuple(p for p in self.params if p.source == "session"),
        )
        object.__setattr__(
            self,
            "needs_session",
            bool(self.session_params) or _uses_session_auth(self.route.access),
        )
        object.__setattr__(
            self,
            "takes_text",
            any(p.text and p.source == "body" for p in self.params),
        )
        # The Request object is built only for an action that can read it: it
        # asked for it, binds the body or files, or may render with a custom
        # renderer. Everything else binds from the parsed values alone.
        object.__setattr__(
            self,
            "needs_request",
            self.takes_request
            or self.needs_body
            or has_files
            or self.needs_session
            or not self.default_json,
        )
        bodies = [p for p in self.params if p.source == "body"]
        only_body = len(bodies) == 1 and not has_files
        object.__setattr__(self, "body_param", bodies[0] if only_body else None)
        object.__setattr__(
            self,
            "native_defaults",
            tuple(
                (p.name, p.default)
                for p in self.params
                if p.source == "query" and type(p.default) is KINDS_BY_NAME.get(p.kind)
            ),
        )

    def complete_native_args(
        self, supplied: dict[str, object], body: tuple[str, object] | None = None
    ) -> dict[str, object]:
        """Restore omitted query defaults, and place the validated body, in
        declaration order."""
        if not self.native_defaults and body is None:
            return supplied
        defaults = dict(self.native_defaults)
        if body is not None:
            defaults[body[0]] = body[1]
        return {
            name: supplied[name] if name in supplied else defaults[name]
            for name in self.native_input_names
        }

    @property
    def owner_headers(self) -> tuple[tuple[str, str], ...]:
        """The owner's response headers, read per request: they may change
        after startup. A deprecated action adds its ``Deprecation`` header."""
        headers = tuple(getattr(self.route.owner, "response_headers", ()))
        notice = header_of(self.route.deprecated)
        return headers if notice is None else (*headers, notice)

    @property
    def query_param(self) -> Param | None:
        return next((p for p in self.params if p.source == "queryschema"), None)

    @property
    def requires_files(self) -> bool:
        return any(
            param.source == "file" and param.file_mode != "optional"
            for param in self.params
        )

    def collect(self, request: RequestData) -> dict[str, object]:
        """Keyword arguments for the handler; raises ``ValidationError`` with
        every problem found."""
        values: dict[str, object] = {}
        errors: list[ErrorDetail] = []
        for param in self.params:
            if param.source in {"resource", "session"}:
                continue
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
        parameters = list(signature_of(route.handler).parameters.values())
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
    return annotation is Caller or typing.get_origin(annotation) is Caller


def checked_pagination(route: Route, name: str, annotation: type) -> Pagination:
    """The route's strategy, which must be the one the parameter type needs."""
    if route.pagination is None:
        raise RegistrationError(
            f"{route.name}: {name!r} asks for {annotation.__name__} but the action "
            "has no pagination=... option"
        )
    wanted = CursorPagination if annotation is CursorParams else PageNumberPagination
    if not isinstance(route.pagination, wanted):
        raise RegistrationError(
            f"{route.name}: {name!r} is {annotation.__name__}, which needs "
            f"pagination={wanted.__name__}(...)"
        )
    return route.pagination


def listing_param(route: Route, name: str, annotation: object) -> Param | None:
    """The page and filter parameters of a list action."""
    if annotation is PageParams or annotation is CursorParams:
        return Param(name, "page", strategy=checked_pagination(route, name, annotation))
    if isinstance(annotation, type) and issubclass(annotation, FilterSet):
        return Param(name, "filters", strategy=route.pagination, filters=annotation)
    return None


def special_param(
    route: Route,
    parameter: inspect.Parameter,
    annotation: object,
) -> Param | None:
    """Parameters that are not plain request values."""
    session = session_param(route, parameter, annotation)
    if session is not None:
        return session
    return regular_special_param(route, parameter, annotation)


def _uses_session_auth(access: object | None) -> bool:
    auth = getattr(access, "auth", None)
    return auth is not None and session_auth(auth)


def regular_special_param(
    route: Route, parameter: inspect.Parameter, annotation: object
) -> Param | None:
    """Recognize non-session framework parameters."""
    name, where = parameter.name, route.name
    special = listing_param(route, name, annotation)
    if special is None:
        special = resource_param(route, parameter, annotation)
    if special is not None:
        return special
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
                "POST/PUT/PATCH/QUERY action, not a path or query parameter"
            )
        annotation.specs()  # unresolvable annotations fail now, not per request
        return Param(name, "body", schema=annotation)
    return None


def session_param(
    route: Route, parameter: inspect.Parameter, annotation: object
) -> Param | None:
    """Recognize request-scoped session parameters before generic Resources."""
    if not getattr(annotation, "__hinoki_session__", False):
        return None
    name = parameter.name
    if parameter.default is not EMPTY:
        raise RegistrationError(
            f"{route.name}: session parameter {name!r} cannot have a default"
        )
    if name in PATH_PARAM.findall(route.path):
        raise RegistrationError(
            f"{route.name}: session parameter {name!r} cannot also be a path parameter"
        )
    return Param(name, "session")


def resource_param(
    route: Route, parameter: inspect.Parameter, annotation: object
) -> Param | None:
    """Recognize a concrete worker resource and reject ambiguous declarations."""
    if not isinstance(annotation, type) or not issubclass(annotation, Resource):
        return None
    name = parameter.name
    if annotation is Resource or inspect.isabstract(annotation):
        raise RegistrationError(
            f"{route.name}: {name!r} must use a concrete Resource subclass"
        )
    if parameter.default is not EMPTY:
        raise RegistrationError(
            f"{route.name}: resource parameter {name!r} cannot have a default"
        )
    if name in PATH_PARAM.findall(route.path):
        raise RegistrationError(
            f"{route.name}: resource parameter {name!r} cannot also be a path parameter"
        )
    return Param(name, "resource", resource_type=annotation)


SOURCE_PARSERS = {
    "form": "form parsers (FormParser, MultiPartParser)",
    "xml": "XMLParser",
}


def accepts_source(source: TextSource, parser: Parser) -> bool:
    return isinstance(parser, XMLParser) if source.kind == "xml" else parser.forms


def text_body_param(
    route: Route,
    parameter: inspect.Parameter,
    annotation: object,
    source: TextSource,
) -> Param:
    """``Form[Schema]``/``XML[Schema]``: the Schema read from text, converted
    to its declared types first."""
    where, name = route.name, parameter.name
    if not (isinstance(annotation, type) and issubclass(annotation, Schema)):
        raise RegistrationError(
            f"{where}: {source.kind}[...] of {name!r} needs a Schema class"
        )
    if name in PATH_PARAM.findall(route.path):
        raise RegistrationError(
            f"{where}: {source.kind}[...] parameter {name!r} cannot be a path parameter"
        )
    annotation.specs()  # unresolvable annotations fail now, not per request
    if source.kind == "query":
        return Param(name, "queryschema", schema=annotation, text=True)
    if route.method not in BODY_METHODS:
        raise RegistrationError(
            f"{where}: {source.kind}[...] parameter {name!r} must be the body of a "
            "POST/PUT/PATCH/QUERY action"
        )
    if not all(accepts_source(source, p) for p in route.parsers):
        raise RegistrationError(
            f"{where}: {source.kind}[...] parameters accept only "
            f"{SOURCE_PARSERS[source.kind]} in the action's parsers=...; omit "
            "parsers= to select the default, and use a plain Schema for JSON"
        )
    return Param(name, "body", schema=annotation, text=True)


def check_query_schema(route: Route, params: tuple[Param, ...]) -> None:
    """``Query[...]`` owns the whole query string: one per action, and no other
    query or page parameters beside it."""
    whole = [p for p in params if p.source == "queryschema"]
    if len(whole) > 1:
        raise RegistrationError(f"{route.name}: at most one Query[...] parameter")
    if whole and any(p.source in {"query", "page"} for p in params):
        raise RegistrationError(
            f"{route.name}: Query[...] reads the whole query string; declare the "
            "other query values as fields of its Schema"
        )


def check_filters(route: Route, params: tuple[Param, ...]) -> None:
    """A ``FilterSet`` reads the whole query string besides the pagination
    parameters: one per GET action, and no other query values beside it. Its
    produced names must not collide with the pagination parameters."""
    found = [p for p in params if p.source == "filters"]
    if not found:
        return
    if route.method != "GET":
        raise RegistrationError(
            f"{route.name}: a FilterSet parameter needs a GET action"
        )
    if len(found) > 1 or any(p.source in {"query", "queryschema"} for p in params):
        raise RegistrationError(
            f"{route.name}: a FilterSet reads the whole query string; declare the "
            "other query values as filters of that FilterSet"
        )
    reserved = route.pagination.names if route.pagination is not None else ()
    for param in found:
        if param.filters is not None:
            param.filters.check_reserved(reserved)


def check_body_and_files(
    route: Route, params: tuple[Param, ...], sources: dict[str, TextSource]
) -> None:
    """One body parameter at most; next to file parameters only ``Form[...]``,
    which reads the text fields of the same multipart body."""
    bodies = [p for p in params if p.source == "body"]
    if len(bodies) > 1:
        raise RegistrationError(f"{route.name}: at most one body parameter")
    if not bodies or not any(p.source == "file" for p in params):
        return
    source = sources.get(bodies[0].name)
    if source is None or source.kind != "form":
        raise RegistrationError(
            f"{route.name}: a Schema body next to file parameters must be "
            "declared Form[...]"
        )


def text_sources(route: Route) -> dict[str, TextSource]:
    """Parameters annotated with a text-source marker, by name. Annotations that
    cannot be resolved answer nothing here; ``bind`` reports them."""
    try:
        hints = hints_of(route.handler, include_extras=True)
    except (NameError, TypeError):
        return {}
    found: dict[str, TextSource] = {}
    for name, hint in hints.items():
        if typing.get_origin(hint) is typing.Annotated:
            marks = [m for m in hint.__metadata__ if isinstance(m, TextSource)]
            if marks:
                found[name] = marks[0]
    return found


def check_scalar_constraints(route: Route, params: tuple[Param, ...]) -> None:
    """Refuse ``Field(...)`` on a scalar path or query parameter: nothing would
    enforce it, so the constraint would be silently ignored. ``bind`` has already
    resolved the annotations, so a failure here cannot happen."""
    scalars = {p.name for p in params if p.source in {"path", "query"}}
    for name, hint in hints_of(route.handler, include_extras=True).items():
        if name in scalars and carries_field(hint):
            raise RegistrationError(
                f"{route.name}: `Field(...)` on the scalar parameter {name!r} "
                "is not enforced, and its description and examples are not read "
                "either; declare the constraints in a `Schema` and take it as "
                "`Query[Schema]` or as the body"
            )


def declared_source(route: Route) -> TextSource | None:
    """The text source a body action binds with ``Form[...]``/``XML[...]``, which
    decides the parsers it needs."""
    if route.method not in BODY_METHODS:
        return None
    marks = text_sources(route).values()
    return next((mark for mark in marks if mark.kind in SOURCE_PARSERS), None)


def file_parameter(
    route: Route, parameter: inspect.Parameter, annotation: object
) -> Param | None:
    """Recognize the closed set of required, optional and repeated upload types."""
    mode = file_mode(route, parameter, annotation)
    if not mode:
        return None
    check_file_parameter(route, parameter, mode)
    return Param(parameter.name, "file", default=parameter.default, file_mode=mode)


def upload_shape(annotation: object) -> str:
    """``required``, ``many`` or ``optional`` for an upload annotation, else empty."""
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if annotation is UploadedFile:
        return "required"
    if origin is list and args == (UploadedFile,):
        return "many"
    is_optional = (
        origin in {typing.Union, UnionType}
        and len(args) == OPTIONAL_UNION_ARITY
        and UploadedFile in args
        and NoneType in args
    )
    return "optional" if is_optional else ""


def file_mode(route: Route, parameter: inspect.Parameter, annotation: object) -> str:
    """The supported upload annotation shape, or empty for other parameters."""
    mode = upload_shape(annotation)
    if mode == "optional" and parameter.default is not None:
        raise RegistrationError(
            f"{route.name}: optional file parameter {parameter.name!r} "
            "must default to None"
        )
    return mode


def declares_upload(route: Route) -> bool:
    """Whether a body action binds an uploaded file, so it needs multipart.

    Unresolvable annotations answer ``False``; ``bind`` then reports them.
    """
    if route.method not in BODY_METHODS:
        return False
    parameters = handler_parameters(route)
    try:
        hints = hints_of(route.handler)
    except (NameError, TypeError):
        return False
    return any(upload_shape(hints.get(item.name)) for item in parameters)


def check_file_parameter(route: Route, parameter: inspect.Parameter, mode: str) -> None:
    """Validate route, field name, default and parser for an upload parameter."""
    if route.method not in BODY_METHODS:
        raise RegistrationError(
            f"{route.name}: file parameter {parameter.name!r} requires a "
            "POST/PUT/PATCH/QUERY action"
        )
    if parameter.name in PATH_PARAM.findall(route.path):
        raise RegistrationError(
            f"{route.name}: file parameter {parameter.name!r} cannot also be "
            "a path parameter"
        )
    if mode == "required" and parameter.default is not EMPTY:
        raise RegistrationError(
            f"{route.name}: required file parameter {parameter.name!r} cannot "
            "have a default"
        )
    if mode == "many" and parameter.default is not EMPTY:
        raise RegistrationError(
            f"{route.name}: repeated file parameter {parameter.name!r} cannot "
            "have a default"
        )
    if not any(isinstance(parser, MultiPartParser) for parser in route.parsers):
        raise RegistrationError(
            f"{route.name}: file parameters require MultiPartParser in the "
            "action's parsers=...; add it, or omit parsers= to select it "
            "automatically"
        )


def make_param(
    route: Route,
    parameter: inspect.Parameter,
    annotation: object,
    source: TextSource | None = None,
) -> Param:
    special = (
        text_body_param(route, parameter, annotation, source)
        if source is not None
        else special_param(route, parameter, annotation)
    )
    if special is not None:
        return special
    upload = file_parameter(route, parameter, annotation)
    if upload is not None:
        return upload
    name = parameter.name
    convert = CONVERTERS.get(annotation)
    if convert is None:
        raise RegistrationError(
            f"{route.name}: unsupported type for parameter {name!r}"
        )
    kind = KINDS[annotation]
    if name in PATH_PARAM.findall(route.path):
        return Param(name, "path", convert, default=parameter.default, kind=kind)
    return Param(name, "query", convert, default=parameter.default, kind=kind)


def split_request(
    route: Route, parameters: list[inspect.Parameter], hints: dict[str, typing.Any]
) -> tuple[bool, list[inspect.Parameter]]:
    """Whether the action declares the request, and the remaining parameters.

    The request is optional: it is passed only to an action that annotates a
    parameter with ``Request``, and then it must be the first one (after
    ``self`` in an API class). Nothing else may be annotated with it, and a
    parameter called ``request`` that is not annotated is a mistake."""
    first = parameters[0] if parameters else None
    takes = first is not None and hints.get(first.name) is Request
    rest = parameters[1:] if takes else parameters
    if (
        takes
        and first is not None
        and first.kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD
    ):
        raise RegistrationError(
            f"{route.name}: the request parameter must be usable by position"
        )
    for parameter in rest:
        if hints.get(parameter.name) is Request:
            raise RegistrationError(
                f"{route.name}: `{parameter.name}: Request` must be the first "
                "parameter (after `self` in an API class)"
            )
        if parameter.name == "request":  # named like the request, not annotated as it
            raise RegistrationError(
                f"{route.name}: annotate the `request` parameter with `Request`, "
                "or remove it: the request is optional and passed only when declared"
            )
    return takes, rest


def describe_annotation(annotation: object) -> str:
    """The return annotation as written, for error messages."""
    if annotation is NO_ANNOTATION:
        return "(none)"
    return getattr(annotation, "__name__", None) or str(annotation)


def bind(route: Route) -> Binding:
    parameters = handler_parameters(route)
    check_owner(route)
    try:
        hints = hints_of(route.handler)
    except (NameError, TypeError) as exc:
        raise RegistrationError(
            f"{route.name}: cannot resolve annotations: {exc}"
        ) from exc
    takes_request, parameters = split_request(route, parameters, hints)
    names = {p.name for p in parameters}
    missing = set(PATH_PARAM.findall(route.path)) - names
    if missing:
        raise RegistrationError(
            f"{route.name}: path parameters not accepted by the handler: "
            f"{sorted(missing)}"
        )
    sources = text_sources(route)
    params = tuple(
        make_param(route, p, hints.get(p.name), sources.get(p.name)) for p in parameters
    )
    resource_types = [p.resource_type for p in params if p.source == "resource"]
    if len(resource_types) != len(set(resource_types)):
        raise RegistrationError(
            f"{route.name}: a Resource type may be requested only once per handler"
        )
    if sum(p.source == "session" for p in params) > 1:
        raise RegistrationError(
            f"{route.name}: a handler may request Session only once"
        )
    if route.pagination is not None and not any(p.source == "page" for p in params):
        raise RegistrationError(
            f"{route.name}: pagination=... needs a PageParams or CursorParams parameter"
        )
    check_scalar_constraints(route, params)
    check_query_schema(route, params)
    check_filters(route, params)
    check_body_and_files(route, params, sources)
    needs_body = any(p.source in {"body", "file"} for p in params)
    response = hints.get("return")
    streams, stream_capable = check_streaming(route, response)
    response_schema = (
        response
        if isinstance(response, type)
        and issubclass(response, Schema)
        and response is not Schema
        else None
    )
    response_check = (
        compile_response_check(route.name, hints.get("return", NO_ANNOTATION))
        if route.validate_response
        else None
    )
    return Binding(
        route,
        params,
        takes_request,
        needs_body,
        response_schema,
        response_check,
        describe_annotation(hints.get("return", NO_ANNOTATION)),
        streams=streams,
        stream_capable=stream_capable,
        narrow_schema=collection_narrow_schema(response),
    )


def collection_narrow_schema(annotation: object) -> type[Schema] | None:
    """The Schema ``S`` that a result is narrowed to when the return annotation is
    ``list[S]``, ``Page[S]``, ``CursorPage[S]`` or ``S | None``; ``None`` for any
    other annotation (a bare ``S`` is ``response_schema``)."""
    arms = [arm for arm in union_arms(annotation) if arm is not NoneType]
    if len(arms) != 1:
        return None
    arm = arms[0]
    if typing.get_origin(arm) not in {list, Page, CursorPage}:
        return arm if arm is not unwrap(annotation) and is_schema(arm) else None
    args = typing.get_args(arm)
    item = unwrap(args[0]) if len(args) == 1 else None
    return item if is_schema(item) else None


def is_schema(candidate: object) -> typing.TypeGuard[type[Schema]]:
    return (
        isinstance(candidate, type)
        and issubclass(candidate, Schema)
        and candidate is not Schema
    )


def check_streaming(route: Route, annotation: object) -> tuple[bool, bool]:
    """Whether the action streams (its annotation names ``Stream``,
    ``EventStream`` or ``File``); ``RegistrationError`` for a declaration that
    cannot work with a stream."""
    handler = route.handler
    if inspect.isasyncgenfunction(handler) or inspect.isgeneratorfunction(handler):
        raise RegistrationError(
            f"{route.name}: a generator function cannot be the handler; "
            "return Stream(...) or EventStream(...) built from the generator, "
            "and annotate the action with it"
        )
    arms = union_arms(annotation)
    streams = any(isinstance(arm, type) and issubclass(arm, Streamed) for arm in arms)
    stream_capable = Response in arms or any(
        isinstance(arm, type) and issubclass(arm, Streamed) for arm in arms
    )
    if streams and route.atomic:
        raise RegistrationError(
            f"{route.name}: atomic=True cannot be combined with a streamed "
            "response; the transaction would stay open for the whole stream"
        )
    if streams and route.status in BODYLESS:
        raise RegistrationError(
            f"{route.name}: status {route.status} cannot have a body"
        )
    return streams, stream_capable
