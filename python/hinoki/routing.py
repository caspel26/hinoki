"""HTTP method decorators.

Decorators only attach metadata; nothing is registered, opened or started
until the function or API class is passed to ``Hinoki.include``. They
return the original callable unchanged, so signatures stay visible to type
checkers. For standalone actions, ``Hinoki.include`` uses the final decorated
callable. In a ``RouteGroup``, put the HTTP decorator outermost so the group
captures the final callable.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, ParamSpec, Protocol, TypedDict, TypeVar, Unpack

from hinoki.access import Access
from hinoki.errors import APIError, TooManyRequests
from hinoki.fields import UNSET, Unset
from hinoki.http.cors import Cors
from hinoki.http.middleware import Middleware, check_middleware
from hinoki.pagination import Pagination
from hinoki.parsers import DEFAULT_PARSERS, Parser, check_parsers
from hinoki.renderers import DEFAULT_RENDERERS, Renderer, check_renderers

if TYPE_CHECKING:
    from hinoki.api import API
    from hinoki.ratelimit import RateLimitPolicy

P = ParamSpec("P")
R = TypeVar("R")

ROUTE_ATTR = "__hinoki_route__"
MIN_STATUS = 200
MAX_STATUS = 599
MIN_ERROR_STATUS = 400
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True, slots=True)
class Route:
    """Declared contract of one action, before it is bound to an application."""

    method: str
    path: str
    handler: Callable[..., object]
    status: int
    access: Access | None
    atomic: bool
    owner: type | None = None
    pagination: Pagination | None = None
    parsers: tuple[Parser, ...] = DEFAULT_PARSERS
    renderers: tuple[Renderer, ...] = DEFAULT_RENDERERS
    errors: tuple[type[APIError], ...] = ()
    validate_response: bool = True
    rate_limit: RateLimitPolicy | None = None
    middleware: tuple[Middleware, ...] = ()
    cors: Cors | None = None
    include_in_schema: bool = True
    tags: tuple[str, ...] = ()
    deprecated: bool | date = False
    operation_id: str | None = None
    access_explicit: bool = False

    @property
    def is_async(self) -> bool:
        return inspect.iscoroutinefunction(self.handler)

    @property
    def name(self) -> str:
        return self.handler.__qualname__


@dataclass(frozen=True, slots=True)
class Options:
    """Keyword options shared by every HTTP method decorator."""

    status: int = 200
    access: Access | None = None
    atomic: bool = False
    pagination: Pagination | None = None
    parsers: Sequence[Parser] | None = None
    renderers: Sequence[Renderer] | None = None
    errors: Sequence[type[APIError]] = ()
    validate_response: bool = True
    rate_limit: RateLimitPolicy | None = None
    middleware: Sequence[Middleware] = ()
    cors: Cors | None = None
    include_in_schema: bool = True
    tags: Sequence[str] = ()
    deprecated: bool | date = False
    operation_id: str | None = None
    access_explicit: bool = False


def _resolve_access(access: Access | Unset | None) -> tuple[Access | None, bool]:
    if isinstance(access, Unset):
        return None, False
    return access, True


class RouteDeclaration:
    """Decorator produced by ``@http.get(...)`` and friends.

    Validates the declaration eagerly and attaches the route to the function
    it decorates, returning that function unchanged.
    """

    def __init__(self, method: str, path: str, options: Options) -> None:
        if not path.startswith("/"):
            raise ValueError(f"route path must start with '/': {path!r}")
        if not MIN_STATUS <= options.status <= MAX_STATUS:
            raise ValueError(f"invalid HTTP status {options.status}")
        self.method = method
        self.path = path
        self.options = options

    def __call__(self, fn: Callable[P, R]) -> Callable[P, R]:
        if hasattr(fn, ROUTE_ATTR):
            raise ValueError(f"{fn.__qualname__} already declares an HTTP route")
        options = self.options
        route = Route(
            self.method,
            self.path,
            fn,
            options.status,
            options.access,
            options.atomic,
            pagination=options.pagination,
            parsers=(
                DEFAULT_PARSERS
                if options.parsers is None
                else check_parsers(options.parsers)
            ),
            renderers=(
                DEFAULT_RENDERERS
                if options.renderers is None
                else check_renderers(options.renderers)
            ),
            errors=check_errors(rate_limited(options)),
            validate_response=options.validate_response,
            rate_limit=options.rate_limit,
            middleware=check_middleware(options.middleware),
            cors=check_cors(options.cors),
            include_in_schema=check_flag(
                "include_in_schema", options.include_in_schema
            ),
            tags=check_tags(options.tags),
            deprecated=check_deprecated(options.deprecated),
            operation_id=check_operation_id(options.operation_id),
            access_explicit=options.access_explicit,
        )
        setattr(fn, ROUTE_ATTR, route)
        return fn


def check_flag(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name}=... takes True or False, got {value!r}")
    return value


def check_deprecated(value: object) -> bool | date:
    """``True``, ``False`` or the ``datetime.date`` the action was deprecated on."""
    if isinstance(value, datetime) or not isinstance(value, bool | date):
        raise ValueError(f"deprecated=... takes True, False or a date, got {value!r}")
    return value


def check_tags(tags: Sequence[str]) -> tuple[str, ...]:
    """The declared OpenAPI tags: non-empty strings, in order, without repeats."""
    if isinstance(tags, str):
        raise ValueError(f"tags=... takes a list of names, not the string {tags!r}")
    found: list[str] = []
    for tag in tags:
        if not isinstance(tag, str) or not tag.strip():
            raise ValueError(f"tags=... takes non-empty strings, got {tag!r}")
        if tag not in found:
            found.append(tag)
    return tuple(found)


def check_operation_id(operation_id: str | None) -> str | None:
    if operation_id is not None and (
        not isinstance(operation_id, str) or not operation_id.strip()
    ):
        raise ValueError(
            f"operation_id=... takes a non-empty string, got {operation_id!r}"
        )
    return operation_id


def check_cors(cors: Cors | None) -> Cors | None:
    if cors is not None and not isinstance(cors, Cors):
        raise ValueError(f"cors=... takes an http.Cors, got {cors!r}")
    return cors


def rate_limited(options: Options) -> Sequence[type[APIError]]:
    """The declared errors; a rate-limited action always documents its 429."""
    if options.rate_limit is None or any(
        isinstance(e, type) and issubclass(e, TooManyRequests) for e in options.errors
    ):
        return options.errors
    return (*options.errors, TooManyRequests)


def check_errors(errors: Sequence[type[APIError]]) -> tuple[type[APIError], ...]:
    """The declared errors, each an ``APIError`` subclass with a 4xx/5xx status."""
    found: list[type[APIError]] = []
    for error in errors:
        if not (isinstance(error, type) and issubclass(error, APIError)):
            raise ValueError(f"errors=... takes APIError subclasses, got {error!r}")
        if not MIN_ERROR_STATUS <= int(error.status) <= MAX_STATUS:
            raise ValueError(f"{error.__name__}.status must be 400 to 599")
        if error not in found:
            found.append(error)
    return tuple(found)


def get(
    path: str,
    *,
    status: int = 200,
    access: Access | Unset | None = UNSET,
    atomic: bool = False,
    pagination: Pagination | None = None,
    parsers: Sequence[Parser] | None = None,
    renderers: Sequence[Renderer] | None = None,
    errors: Sequence[type[APIError]] = (),
    validate_response: bool = True,
    rate_limit: RateLimitPolicy | None = None,
    middleware: Sequence[Middleware] = (),
    cors: Cors | None = None,
    include_in_schema: bool = True,
    tags: Sequence[str] = (),
    deprecated: bool | date = False,
    operation_id: str | None = None,
) -> RouteDeclaration:
    """Declare a GET action. ``errors`` lists the ``APIError`` subclasses the
    action may raise; each carries its own status and code, which OpenAPI and the
    MCP tool description then show."""
    resolved_access, access_explicit = _resolve_access(access)
    options = Options(
        status,
        resolved_access,
        atomic,
        pagination,
        parsers,
        renderers,
        errors,
        validate_response,
        rate_limit,
        middleware,
        cors,
        include_in_schema,
        tags,
        deprecated,
        operation_id,
        access_explicit,
    )
    return RouteDeclaration("GET", path, options)


def post(
    path: str,
    *,
    status: int = 200,
    access: Access | Unset | None = UNSET,
    atomic: bool = False,
    pagination: Pagination | None = None,
    parsers: Sequence[Parser] | None = None,
    renderers: Sequence[Renderer] | None = None,
    errors: Sequence[type[APIError]] = (),
    validate_response: bool = True,
    rate_limit: RateLimitPolicy | None = None,
    middleware: Sequence[Middleware] = (),
    cors: Cors | None = None,
    include_in_schema: bool = True,
    tags: Sequence[str] = (),
    deprecated: bool | date = False,
    operation_id: str | None = None,
) -> RouteDeclaration:
    """Declare a POST action. ``errors`` lists the ``APIError`` subclasses the
    action may raise; each carries its own status and code, which OpenAPI and the
    MCP tool description then show."""
    resolved_access, access_explicit = _resolve_access(access)
    options = Options(
        status,
        resolved_access,
        atomic,
        pagination,
        parsers,
        renderers,
        errors,
        validate_response,
        rate_limit,
        middleware,
        cors,
        include_in_schema,
        tags,
        deprecated,
        operation_id,
        access_explicit,
    )
    return RouteDeclaration("POST", path, options)


def put(
    path: str,
    *,
    status: int = 200,
    access: Access | Unset | None = UNSET,
    atomic: bool = False,
    pagination: Pagination | None = None,
    parsers: Sequence[Parser] | None = None,
    renderers: Sequence[Renderer] | None = None,
    errors: Sequence[type[APIError]] = (),
    validate_response: bool = True,
    rate_limit: RateLimitPolicy | None = None,
    middleware: Sequence[Middleware] = (),
    cors: Cors | None = None,
    include_in_schema: bool = True,
    tags: Sequence[str] = (),
    deprecated: bool | date = False,
    operation_id: str | None = None,
) -> RouteDeclaration:
    """Declare a PUT action. ``errors`` lists the ``APIError`` subclasses the
    action may raise; each carries its own status and code, which OpenAPI and the
    MCP tool description then show."""
    resolved_access, access_explicit = _resolve_access(access)
    options = Options(
        status,
        resolved_access,
        atomic,
        pagination,
        parsers,
        renderers,
        errors,
        validate_response,
        rate_limit,
        middleware,
        cors,
        include_in_schema,
        tags,
        deprecated,
        operation_id,
        access_explicit,
    )
    return RouteDeclaration("PUT", path, options)


def patch(
    path: str,
    *,
    status: int = 200,
    access: Access | Unset | None = UNSET,
    atomic: bool = False,
    pagination: Pagination | None = None,
    parsers: Sequence[Parser] | None = None,
    renderers: Sequence[Renderer] | None = None,
    errors: Sequence[type[APIError]] = (),
    validate_response: bool = True,
    rate_limit: RateLimitPolicy | None = None,
    middleware: Sequence[Middleware] = (),
    cors: Cors | None = None,
    include_in_schema: bool = True,
    tags: Sequence[str] = (),
    deprecated: bool | date = False,
    operation_id: str | None = None,
) -> RouteDeclaration:
    """Declare a PATCH action. ``errors`` lists the ``APIError`` subclasses the
    action may raise; each carries its own status and code, which OpenAPI and the
    MCP tool description then show."""
    resolved_access, access_explicit = _resolve_access(access)
    options = Options(
        status,
        resolved_access,
        atomic,
        pagination,
        parsers,
        renderers,
        errors,
        validate_response,
        rate_limit,
        middleware,
        cors,
        include_in_schema,
        tags,
        deprecated,
        operation_id,
        access_explicit,
    )
    return RouteDeclaration("PATCH", path, options)


def delete(
    path: str,
    *,
    status: int = 200,
    access: Access | Unset | None = UNSET,
    atomic: bool = False,
    pagination: Pagination | None = None,
    parsers: Sequence[Parser] | None = None,
    renderers: Sequence[Renderer] | None = None,
    errors: Sequence[type[APIError]] = (),
    validate_response: bool = True,
    rate_limit: RateLimitPolicy | None = None,
    middleware: Sequence[Middleware] = (),
    cors: Cors | None = None,
    include_in_schema: bool = True,
    tags: Sequence[str] = (),
    deprecated: bool | date = False,
    operation_id: str | None = None,
) -> RouteDeclaration:
    """Declare a DELETE action. ``errors`` lists the ``APIError`` subclasses the
    action may raise; each carries its own status and code, which OpenAPI and the
    MCP tool description then show."""
    resolved_access, access_explicit = _resolve_access(access)
    options = Options(
        status,
        resolved_access,
        atomic,
        pagination,
        parsers,
        renderers,
        errors,
        validate_response,
        rate_limit,
        middleware,
        cors,
        include_in_schema,
        tags,
        deprecated,
        operation_id,
        access_explicit,
    )
    return RouteDeclaration("DELETE", path, options)


def query(
    path: str,
    *,
    status: int = 200,
    access: Access | Unset | None = UNSET,
    atomic: bool = False,
    pagination: Pagination | None = None,
    parsers: Sequence[Parser] | None = None,
    renderers: Sequence[Renderer] | None = None,
    errors: Sequence[type[APIError]] = (),
    validate_response: bool = True,
    rate_limit: RateLimitPolicy | None = None,
    middleware: Sequence[Middleware] = (),
    cors: Cors | None = None,
    include_in_schema: bool = True,
    tags: Sequence[str] = (),
    deprecated: bool | date = False,
    operation_id: str | None = None,
) -> RouteDeclaration:
    """Declare a QUERY action: a safe, idempotent request that carries a body
    (a search too large or too structured for a query string). It binds its
    body like POST and is never wrapped in a transaction unless asked.
    ``errors`` lists the ``APIError`` subclasses the
    action may raise; each carries its own status and code, which OpenAPI and the
    MCP tool description then show."""
    resolved_access, access_explicit = _resolve_access(access)
    options = Options(
        status,
        resolved_access,
        atomic,
        pagination,
        parsers,
        renderers,
        errors,
        validate_response,
        rate_limit,
        middleware,
        cors,
        include_in_schema,
        tags,
        deprecated,
        operation_id,
        access_explicit,
    )
    return RouteDeclaration("QUERY", path, options)


class Mount:
    """Places routes under a path prefix chosen where the application is declared.

        Hinoki(include=[
            health,
            Mount("/tasks", tasks.routes),
            Mount("/billing", billing.routes),
        ])

    The feature keeps paths relative to itself (``/`` and ``/{id}``) and the root
    declaration shows where each feature lives. Items are anything ``include``
    accepts, including lists and other mounts; prefixes join outside in.
    """

    def __init__(self, prefix: str, *items: object) -> None:
        if not prefix.startswith("/") or prefix.endswith("/") or prefix == "":
            raise ValueError("a mount prefix starts with '/' and does not end with one")
        if not items:
            raise ValueError("a Mount needs at least one item")
        self.prefix = prefix
        self.items = items


class GroupActionOptions(TypedDict, total=False):
    """Typed keyword options accepted by ``RouteGroup`` action decorators."""

    status: int
    access: Access | None
    atomic: bool | None
    pagination: Pagination | None
    parsers: Sequence[Parser] | None
    renderers: Sequence[Renderer] | None
    errors: Sequence[type[APIError]]
    validate_response: bool
    rate_limit: RateLimitPolicy | None
    middleware: Sequence[Middleware]
    cors: Cors | None
    include_in_schema: bool
    tags: Sequence[str]
    deprecated: bool | date
    operation_id: str | None


class RouteGroup:
    """A function-based route group with a shared path prefix and defaults.

    Decorated functions belong to this group and the group can be included once
    in a ``Hinoki`` application. Use ``API`` for class-based resources.

        tasks = RouteGroup("/tasks", access=member_only, atomic=True,
                           errors=(NotFound,))

        @tasks.get("/")                      # GET /tasks
        async def listing(...): ...

        @tasks.post("/", status=201)         # POST /tasks, atomic=True
        async def create(...): ...

        @tasks.get("/health", access=None)   # public on purpose, overriding the group

    ``prefix`` is prepended to every path (``"/"`` means the prefix itself),
    ``access`` is the default policy (pass ``access=None`` on an action to make it
    public), ``atomic=True`` makes POST/PUT/PATCH/DELETE transactional unless the
    action says ``atomic=False``, ``errors`` are added to each action's own and
    ``cors`` is the default ``http.Cors`` (``cors=None`` on an action keeps the
    application's policy). ``include_in_schema=False`` leaves every action out of
    the OpenAPI document unless it says ``include_in_schema=True``, and ``tags`` come
    before the tags of each action.
    The result is an ordinary action: rules, OpenAPI and the MCP see no difference.
    """

    def __init__(
        self,
        prefix: str = "",
        *,
        access: Access | None = None,
        atomic: bool = False,
        errors: Sequence[type[APIError]] = (),
        cors: Cors | None = None,
        include_in_schema: bool = True,
        tags: Sequence[str] = (),
    ) -> None:
        if prefix and (not prefix.startswith("/") or prefix.endswith("/")):
            raise ValueError("a prefix starts with '/' and does not end with one")
        self.prefix = prefix
        self.access = access
        self.atomic = atomic
        self.errors = tuple(errors)
        self.cors = check_cors(cors)
        self.include_in_schema = check_flag("include_in_schema", include_in_schema)
        self.tags = check_tags(tags)
        self._handlers: list[Callable[..., object]] = []
        self._included = False

    @property
    def handlers(self) -> tuple[Callable[..., object], ...]:
        """The decorated handlers in declaration order."""
        return tuple(self._handlers)

    def _include(self) -> None:
        self._included = True

    def _decorate(
        self, declaration: RouteDeclaration, fn: Callable[P, R]
    ) -> Callable[P, R]:
        if self._included:
            raise RuntimeError("a RouteGroup cannot change after it is included")
        decorated = declaration(fn)
        self._handlers.append(decorated)
        return decorated

    def declare(
        self, method: str, path: str, options: dict[str, Any]
    ) -> RouteDeclaration:
        if not path.startswith("/"):
            raise ValueError(f"route path must start with '/': {path!r}")
        full = (self.prefix or "/") if path == "/" else self.prefix + path
        access = options.pop("access", UNSET)
        atomic = options.pop("atomic", None)
        cors = options.pop("cors", UNSET)
        own_errors = tuple(options.pop("errors", ()))
        chosen = Options(
            status=options.pop("status", 200),
            access=self.access if isinstance(access, Unset) else access,
            atomic=(
                atomic
                if atomic is not None
                else self.atomic and method in WRITE_METHODS
            ),
            pagination=options.pop("pagination", None),
            parsers=options.pop("parsers", None),
            renderers=options.pop("renderers", None),
            errors=(*self.errors, *own_errors),
            validate_response=options.pop("validate_response", True),
            rate_limit=options.pop("rate_limit", None),
            middleware=options.pop("middleware", ()),
            cors=self.cors if isinstance(cors, Unset) else cors,
            include_in_schema=options.pop("include_in_schema", self.include_in_schema),
            tags=(*self.tags, *options.pop("tags", ())),
            deprecated=options.pop("deprecated", False),
            operation_id=options.pop("operation_id", None),
            access_explicit=not isinstance(access, Unset),
        )
        if options:
            raise TypeError(f"unknown action options {sorted(options)}")
        return RouteDeclaration(method, full, chosen)

    def get(
        self, path: str, **options: Unpack[GroupActionOptions]
    ) -> _GroupRouteDeclaration:
        return _GroupRouteDeclaration(self, self.declare("GET", path, dict(options)))

    def post(
        self, path: str, **options: Unpack[GroupActionOptions]
    ) -> _GroupRouteDeclaration:
        return _GroupRouteDeclaration(self, self.declare("POST", path, dict(options)))

    def put(
        self, path: str, **options: Unpack[GroupActionOptions]
    ) -> _GroupRouteDeclaration:
        return _GroupRouteDeclaration(self, self.declare("PUT", path, dict(options)))

    def patch(
        self, path: str, **options: Unpack[GroupActionOptions]
    ) -> _GroupRouteDeclaration:
        return _GroupRouteDeclaration(self, self.declare("PATCH", path, dict(options)))

    def delete(
        self, path: str, **options: Unpack[GroupActionOptions]
    ) -> _GroupRouteDeclaration:
        return _GroupRouteDeclaration(self, self.declare("DELETE", path, dict(options)))

    def query(
        self, path: str, **options: Unpack[GroupActionOptions]
    ) -> _GroupRouteDeclaration:
        return _GroupRouteDeclaration(self, self.declare("QUERY", path, dict(options)))


class _GroupRouteDeclaration:
    """Attach a group's route metadata and retain the handler for inclusion."""

    def __init__(self, group: RouteGroup, declaration: RouteDeclaration) -> None:
        self.group = group
        self.declaration = declaration

    def __call__(self, fn: Callable[P, R]) -> Callable[P, R]:
        return self.group._decorate(self.declaration, fn)


@dataclass(frozen=True, slots=True)
class Found:
    """The router matched an action."""

    action: int
    params: dict[str, str]


@dataclass(frozen=True, slots=True)
class MethodNotAllowed:
    """The path exists, but not for this method."""

    allowed: list[str]


def route_of(obj: object) -> Route | None:
    if not callable(obj):
        return None
    found = getattr(obj, ROUTE_ATTR, None)
    if not isinstance(found, Route):
        return None
    # functools.wraps copies route metadata, but the stored declaration still
    # points at the original function. Dispatch through the final wrapper.
    return found if found.handler is obj else replace(found, handler=obj)


class ExposesRoutes(Protocol):
    """An object that serves its routes through ``api()``, such as the admin."""

    def api(self) -> type[API]: ...


type Includable = (
    type[API]
    | Callable[..., object]
    | RouteGroup
    | Mount
    | ExposesRoutes
    | Sequence[Includable]
)
"""What ``Hinoki(include=[...])`` and ``app.include(...)`` accept: an ``API`` class,
a decorated function, a ``RouteGroup``, a ``Mount``, an object with ``api()`` (the
admin) or a list or tuple of those."""
