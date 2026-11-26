"""Class-based CRUD generation, composable with ordinary API methods."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import enum
import functools
import inspect
import os
import sys
import types
import typing
import warnings
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date
from operator import attrgetter
from typing import Any, ClassVar, Literal, NoReturn, cast
from uuid import UUID

from hinoki import context
from hinoki.access import Access
from hinoki.api import API
from hinoki.auth import BaseAuth, Caller
from hinoki.errors import (
    ErrorDetail,
    FieldError,
    Forbidden,
    OwnerOutOfScope,
    RegistrationError,
    ValidationError,
)
from hinoki.fields import UNSET, Unset
from hinoki.filters import FilterSet
from hinoki.handlerhints import hints_of, signature_of
from hinoki.http.cors import Cors
from hinoki.http.request import Request
from hinoki.http.response import Response
from hinoki.orm import Model, QuerySet
from hinoki.orm.context import current_database
from hinoki.orm.keyset import CursorRequest, cursor_order, fetch_request, prepare
from hinoki.orm.queryset import TEXT_DECIMAL_OPERATORS, decimal_columns
from hinoki.orm.relations import LinkEnd
from hinoki.pagination import (
    CursorPage,
    CursorPagination,
    CursorParams,
    Page,
    PageNumberPagination,
    PageParams,
    Pagination,
)
from hinoki.routing import (
    Options,
    Route,
    RouteDeclaration,
    check_deprecated,
    check_flag,
    check_tags,
    route_of,
)
from hinoki.schema import Schema
from hinoki.websocket import WebSocketRoute, websocket_of

if typing.TYPE_CHECKING:
    from hinoki.runtime.rowplan import RowWireSelection

ITEM = "/{item_id}"
GENERATED = "hinoki_generated"
ACTIONS = ("list", "retrieve", "create", "update", "delete")
WRITE_ACTIONS = ("create", "update", "delete")
HOOKS = (
    "get_queryset",
    "filter_queryset",
    "to_schema",
    "written_queryset",
    "store",
    "change",
    "remove",
    "link_queryset",
)
INTERNAL_HOOKS = ("reload_written",)


class Crud(enum.StrEnum):
    """The generated actions ``replaces=`` can name (each is also its plain string)."""

    LIST = "list"
    RETRIEVE = "retrieve"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


def caller_id(user: object) -> NoReturn:
    """Marker for ``identity_key``: the owner value is ``Caller.id``, so the
    caller needs no user object. It is never called."""
    raise TypeError("caller_id is read from the Caller, not called")  # pragma: no cover


@dataclasses.dataclass(frozen=True, slots=True)
class UserAttr:
    """Declarative attribute extractor returned by ``from_user(...)``."""

    attr: str
    _getter: Callable[[Any], Any] = dataclasses.field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.attr, str)
            or not self.attr
            or not all(part.isidentifier() for part in self.attr.split("."))
        ):
            raise ValueError(
                f"from_user({self.attr!r}) requires a valid attribute name "
                "or dotted path"
            )
        object.__setattr__(self, "_getter", attrgetter(self.attr))

    def __call__(self, user: Any) -> Any:
        return self._getter(user)


def from_user(attr: str) -> UserAttr:
    """Extract ``attr`` (or a dotted attribute path) from ``Caller.user`` for
    ``crud(..., assign={...})`` or ``identity_key=...``."""
    return UserAttr(attr)


def _coerce_scope_value(kind: str, raw: Any) -> Any:
    coercers: dict[str, Callable[[Any], Any]] = {
        "str": lambda val: val if isinstance(val, str) and val else None,
        "int": lambda val: val if type(val) is int else None,
        "uuid": _coerce_uuid_scope,
        "bool": lambda val: val if type(val) is bool else None,
    }
    if raw is None:
        return None
    coerce = coercers.get(kind)
    return coerce(raw) if coerce is not None else raw


def _coerce_uuid_scope(raw: Any) -> UUID | None:
    resolved: UUID | None = raw if isinstance(raw, UUID) else None
    if resolved is None and isinstance(raw, str):
        with contextlib.suppress(ValueError):
            resolved = UUID(raw)
    return resolved


@dataclasses.dataclass(frozen=True, slots=True)
class RowScope[M: Model, U]:
    """Declarative mandatory row boundary for CRUD, manual actions, and links.

    Instantiate via :meth:`by`, :meth:`by_claim`, or :meth:`by_caller`.
    """

    model: type[M]
    column: str
    _source: Literal["user", "claim", "caller"]
    _key: UserAttr | Callable[[U], Any] | str | None = None

    def __post_init__(self) -> None:
        if not (isinstance(self.model, type) and issubclass(self.model, Model)):
            raise RegistrationError("RowScope model must be a Model subclass")
        if self.model.__abstract__:
            raise RegistrationError(
                f"{self.model.__name__} is abstract; RowScope requires a concrete Model"
            )
        field_type = self.model.__field_types__.get(self.column)
        if field_type is None:
            raise RegistrationError(
                f"{self.model.__name__} has no column {self.column!r} for RowScope"
            )
        if self._source == "caller" and field_type.kind != "str":
            raise RegistrationError(
                f"RowScope.by_caller({self.model.__name__}, {self.column!r}) "
                "requires a text column"
            )
        if self._source == "claim" and (
            not isinstance(self._key, str) or not self._key
        ):
            raise RegistrationError(
                "RowScope.by_claim claim name must be a non-empty string"
            )
        if self._source == "user" and not callable(self._key):
            raise RegistrationError(
                "RowScope.by requires a callable key or from_user(...)"
            )

    @classmethod
    def by(
        cls,
        model: type[M],
        column: str,
        key: UserAttr | Callable[[U], Any],
    ) -> RowScope[M, U]:
        """Scope rows of ``model`` by ``column == key(caller.user)``."""
        return cls(model=model, column=column, _source="user", _key=key)

    @classmethod
    def by_claim(cls, model: type[M], column: str, claim: str) -> RowScope[M, Any]:
        """Scope rows of ``model`` by ``column == caller.claims[claim]``."""
        return cls(model=model, column=column, _source="claim", _key=claim)

    @classmethod
    def by_caller(cls, model: type[M], column: str) -> RowScope[M, Any]:
        """Scope rows of ``model`` by ``column == caller.id``."""
        return cls(model=model, column=column, _source="caller", _key=None)

    def value_for(self, caller: Caller[U] | None) -> Any:
        """Resolve and validate the mandatory scope value for ``caller``."""
        if caller is None:
            raise Forbidden("You cannot access this item.")
        if self._source == "caller":
            raw: Any = caller.id
        elif self._source == "claim":
            raw = caller.claims.get(cast(str, self._key))
        else:
            if caller.user is None:
                raise Forbidden("You cannot access this item.")
            raw = cast(Callable[[U], Any], self._key)(caller.user)
        kind = self.model.__field_types__[self.column].kind
        value = _coerce_scope_value(kind, raw)
        if value is None:
            raise Forbidden("You cannot access this item.")
        return value

    def apply(self, query: QuerySet[M], caller: Caller[U] | None) -> QuerySet[M]:
        """AND the mandatory row scope onto ``query`` for ``caller``."""
        if not isinstance(query, QuerySet) or query.model is not self.model:
            got = getattr(
                getattr(query, "model", None), "__name__", type(query).__name__
            )
            raise RegistrationError(
                f"RowScope({self.model.__name__}, {self.column!r}) expected a "
                f"{self.model.__name__} QuerySet, got {got}"
            )
        return query.filter(**{self.column: self.value_for(caller)})

    def for_caller(self, caller: Caller[U] | None) -> QuerySet[M]:
        """Return ``model.all()`` filtered by this scope for ``caller``."""
        return self.apply(self.model.all(), caller)


HOOK_CALLER_INDEX: dict[str, int] = {
    "get_queryset": 0,
    "filter_queryset": 2,
    "written_queryset": 0,
    "store": 1,
    "change": 3,
    "remove": 2,
}
HOOK_ACTIONS: dict[str, tuple[str, ...]] = {
    "get_queryset": ("list", "retrieve", "update", "delete"),
    "filter_queryset": ("list",),
    "written_queryset": ("create", "update"),
    "store": ("create",),
    "change": ("update",),
    "remove": ("delete",),
}


def _type_name(annotation: Any) -> str:
    return str(getattr(annotation, "__name__", annotation))


def _caller_annotation_info(annotation: Any) -> tuple[bool, bool, Any]:
    """Return ``(is_caller, accepts_none, user_type)`` for a hook annotation."""
    if annotation is Caller:
        return True, False, Any
    if typing.get_origin(annotation) is Caller:
        args = typing.get_args(annotation)
        return True, False, args[0] if args else Any
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        args = typing.get_args(annotation)
        has_none = type(None) in args
        non_none = [arg for arg in args if arg is not type(None)]
        if len(non_none) == 1 and (
            non_none[0] is Caller or typing.get_origin(non_none[0]) is Caller
        ):
            caller_args = typing.get_args(non_none[0])
            return True, has_none, caller_args[0] if caller_args else Any
    return False, True, Any


def _auth_user_type(auth: BaseAuth[Any]) -> Any:
    """Resolve the concrete user type ``T`` from a parameterized ``BaseAuth[T]``."""
    for cls in type(auth).__mro__:
        for base in getattr(cls, "__orig_bases__", ()):
            origin = typing.get_origin(base)
            if isinstance(origin, type) and issubclass(origin, BaseAuth):
                args = typing.get_args(base)
                if args and not isinstance(args[0], typing.TypeVar):
                    return args[0]
    return Any


def _concrete_types(annotation: Any) -> tuple[type, ...] | None:
    """Return concrete ``type`` members for assignability checks, or ``None``."""
    if annotation in (Any, object) or isinstance(annotation, typing.TypeVar):
        return None
    if isinstance(annotation, type):
        return (annotation,)
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        args = typing.get_args(annotation)
        if args and all(isinstance(arg, type) and arg is not object for arg in args):
            return tuple(args)
    return None


def _user_type_compatible(provided: Any, expected: Any) -> bool:
    prov = _concrete_types(provided)
    exp = _concrete_types(expected)
    if prov is None or exp is None:
        return True
    return all(any(issubclass(p, e) for e in exp) for p in prov)


def _check_hook_actions(
    cls_name: str,
    hook_name: str,
    config: _CrudConfig,
    active: list[str],
    accepts_none: bool,
    user_type: Any,
) -> None:
    if not accepts_none:
        public = [action for action in active if config.access_for(action) is None]
        if public:
            actions_text = ", ".join(public)
            raise RegistrationError(
                f"{cls_name}.{hook_name} requires a non-None Caller, "
                f"but generated action(s) {actions_text} are public — "
                "add access=... or annotate the parameter as Caller[...] | None"
            )
    for action in active:
        policy = config.access_for(action)
        if policy is None:
            continue
        auth_type = _auth_user_type(policy.auth)
        if not _user_type_compatible(auth_type, user_type):
            raise RegistrationError(
                f"{cls_name}.{hook_name} expects Caller[{_type_name(user_type)}], "
                f"but action {action!r} authenticates Caller[{_type_name(auth_type)}]"
            )


def _hook_caller_param(
    cls: type[CrudBase], hook_name: str, param_index: int
) -> tuple[inspect.Parameter, Any] | None:
    hook = getattr(cls, hook_name)
    if hook is getattr(CrudBase, hook_name):
        return None
    params = list(signature_of(hook).parameters.values())
    if not isinstance(inspect.getattr_static(cls, hook_name), staticmethod):
        params = params[1:]
    if param_index >= len(params) or params[param_index].kind in (
        inspect.Parameter.VAR_POSITIONAL,
        inspect.Parameter.VAR_KEYWORD,
    ):
        return None
    param = params[param_index]
    try:
        hints = hints_of(hook)
    except (NameError, TypeError):
        return None
    return (param, hints[param.name]) if param.name in hints else None


def _check_hook_caller(
    cls: type[CrudBase], config: _CrudConfig, hook_name: str, param_index: int
) -> None:
    found = _hook_caller_param(cls, hook_name, param_index)
    if found is None:
        return
    param, annotation = found
    is_caller, accepts_none, user_type = _caller_annotation_info(annotation)
    if not is_caller:
        return
    accepts_none = accepts_none or param.default is None
    active = [
        action
        for action in HOOK_ACTIONS[hook_name]
        if action not in config.omit and enabled(config, action)
    ]
    _check_hook_actions(
        cls.__name__, hook_name, config, active, accepts_none, user_type
    )


def check_hook_callers(cls: type[CrudBase], config: _CrudConfig) -> None:
    """Validate overridden hook ``Caller`` annotations against generated actions."""
    for hook_name, param_index in HOOK_CALLER_INDEX.items():
        _check_hook_caller(cls, config, hook_name, param_index)


_PACKAGE_PREFIXES = (
    os.path.dirname(os.path.abspath(__file__)) + os.sep,
    types.__file__,
)


def _user_stacklevel() -> int:
    """Stack level of the first frame outside the hinoki package.

    Warnings are attributed to user code, so per-module filters and ``-W error``
    tracebacks point at the declaration. ``types.new_class`` is skipped too: it
    sits between ``crud(...)`` and the class creation. Computed by hand because
    ``skip_file_prefixes`` mishandles several prefixes on Python 3.13.
    """
    level = 1
    frame: types.FrameType | None = sys._getframe(1)
    while frame is not None and frame.f_code.co_filename.startswith(_PACKAGE_PREFIXES):
        frame = frame.f_back
        level += 1
    return level


def warn_unscoped_owner_reads(cls: type[CrudBase], config: _CrudConfig) -> None:
    """Warn when ``owner=`` guards writes but nothing scopes the generated reads."""
    reads = {"list", "retrieve"} - config.omit
    if not config.owner or not reads or cls.get_queryset is not CrudBase.get_queryset:
        return
    warnings.warn(
        f"{config.model.__name__}: owner={config.owner!r} constrains writes only; "
        f"{' and '.join(sorted(reads))} return every owner's rows. Scope reads in "
        "a get_queryset override (class form), or omit=... the read actions; "
        "overriding get_queryset also silences this warning",
        UserWarning,
        stacklevel=_user_stacklevel(),
    )


@dataclasses.dataclass(frozen=True, kw_only=True)
class _CrudConfig:
    """Validated snapshot of a ``crud(...)`` declaration."""

    model: type[Model]
    read: type[Schema]
    create: type[Schema] | None = None
    update: type[Schema] | None = None
    deletable: bool = False
    access: Access | None = None
    read_access: Access | None = None
    public_writes: bool = False
    owner: str = ""
    identity_key: Callable[[Any], Any] = attrgetter("id")
    assign: Mapping[str, Callable[[Any], Any]] = dataclasses.field(default_factory=dict)
    pagination: Pagination = PageNumberPagination()  # noqa: RUF009
    filters: type[FilterSet] | None = None
    actions: Mapping[str, Access | None] | None = None
    omit: frozenset[str] = frozenset()
    cors: Cors | None = None
    schema_shown: Mapping[str, bool] = dataclasses.field(default_factory=dict)
    tags: tuple[str, ...] = ()
    deprecated: bool | date = False
    row_scope: RowScope[Any, Any] | None = None
    links: Mapping[str, Callable[[Any], QuerySet[Any]]] = dataclasses.field(
        default_factory=dict
    )
    link_scopes: Mapping[str, RowScope[Any, Any]] = dataclasses.field(
        default_factory=dict
    )

    @functools.cached_property
    def link_ids(self) -> dict[str, LinkEnd]:
        """The many-to-many relations the model declares, by their ids field."""
        return {
            side.ids_name: side
            for side in self.model.__links__.values()
            if side.forward and side.ids_name
        }

    @functools.cached_property
    def read_links(self) -> tuple[str, ...]:
        return tuple(self.link_ids.keys() & self.read.specs().keys())

    def link_fields(self, schema: type[Schema] | None) -> list[LinkEnd]:
        """The relations whose ids field ``schema`` carries."""
        if schema is None:
            return []
        return [side for name, side in self.link_ids.items() if name in schema.specs()]

    def documentation(self, action: str) -> dict[str, Any]:
        """The OpenAPI options of one generated action."""
        return {
            "include_in_schema": self.schema_shown.get(action, True),
            "tags": self.tags,
            "deprecated": self.deprecated,
        }

    def access_for(self, action: str) -> Access | None:
        """The policy of one generated action: its own entry when ``crud(...)``
        declared per-action access, else the write or read policy."""
        if self.actions is not None:
            return self.actions[action]
        return self.access if action in WRITE_ACTIONS else self.read_access

    def unprotected_writes(self) -> bool:
        return any(self.access_for(a) is None for a in self.writes() or ["create"])

    def is_protected(self) -> bool:
        """Whether any generated CRUD action declares an ``Access`` policy."""
        return any(
            self.access_for(action) is not None
            for action in ACTIONS
            if action not in self.omit and enabled(self, action)
        )

    def assigned(self) -> dict[str, Callable[[Any], Any]]:
        extra = {self.owner: self.identity_key} if self.owner else {}
        return {**self.assign, **extra}

    def server_columns(self) -> set[str]:
        cols = set(self.assigned())
        if self.row_scope is not None:
            cols.add(self.row_scope.column)
        return cols

    def assign_values(self, principal: Any) -> dict[str, Any]:
        assigned_values: dict[str, Any] = {}
        if self.row_scope is not None:
            assigned_values[self.row_scope.column] = self.row_scope.value_for(principal)
        for column, derive in self.assigned().items():
            assigned_values[column] = (
                self.owner_of(principal)
                if column == self.owner
                else derive(principal.user)
            )
        return assigned_values

    def writes(self) -> list[str]:
        found = ["create"] if self.create else []
        found += ["update"] if self.update else []
        found += ["delete"] if self.deletable else []
        return found

    def check(self, *, custom_output: bool = False, custom_links: bool = False) -> None:
        """Fail at class creation, not on the first request."""
        name = self.model.__name__
        columns = set(self.model.__columns__)
        if self.model.__abstract__:
            raise RegistrationError(
                f"{name} is abstract and has no table; generate CRUD for a model "
                "that subclasses it"
            )
        if self.writes() and self.unprotected_writes() and not self.public_writes:
            raise RegistrationError(
                f"{name}: write actions need access=... (or public_writes=True)"
            )
        if self.assigned() and self.unprotected_writes():
            raise RegistrationError(f"{name}: owner/assign need an access policy")
        for column in self.assigned():
            if column not in columns:
                raise RegistrationError(f"{name} has no column {column!r} to assign")
        self.check_row_scope(columns)
        self.check_schemas(columns, custom_output=custom_output)
        self.check_links(custom=custom_links)
        self.check_filters()

    def check_row_scope(self, columns: set[str]) -> None:
        if self.row_scope is None:
            return
        name = self.model.__name__
        scope = self.row_scope
        if scope.model is not self.model:
            raise RegistrationError(
                f"{name}: scope= belongs to {scope.model.__name__}, not {name}"
            )
        if scope.column not in columns or scope.column == self.model.__primary_key__:
            raise RegistrationError(
                f"{name}: scope= column {scope.column!r} must be a "
                "non-primary-key column"
            )
        active = [
            action
            for action in ACTIONS
            if action not in self.omit and enabled(self, action)
        ]
        if any(self.access_for(action) is None for action in active):
            raise RegistrationError(
                f"{name}: scope= needs an access policy on every active action"
            )
        if self.owner and scope.column == self.owner:
            raise RegistrationError(
                f"{name}: scope= column {scope.column!r} cannot also be owner="
            )
        if scope.column in self.assign:
            raise RegistrationError(
                f"{name}: assign= cannot set scope column {scope.column!r}"
            )

    def check_links(self, *, custom: bool) -> None:
        """Every ids field needs a scope for its targets, or the CRUD would let
        a caller link rows it cannot see."""
        name = self.model.__name__
        declared = {side.declared for side in self.link_ids.values()}
        configured = set(self.links) | set(self.link_scopes)
        unknown = sorted(configured - declared)
        if unknown:
            raise RegistrationError(
                f"{name}: links= names {unknown}, which are not many_to_many "
                "relations of the model"
            )
        for schema in (self.read, self.create, self.update):
            if schema is None:
                continue
            for side in self.link_fields(schema):
                self.check_link_field(schema, side, custom=custom)

    def check_link_field(
        self, schema: type[Schema], side: LinkEnd, *, custom: bool
    ) -> None:
        name = self.model.__name__
        configured = set(self.links) | set(self.link_scopes)
        if side.declared not in configured and not custom:
            raise RegistrationError(
                f"crud({name}): {side.ids_name} needs links="
                f'{{"{side.declared}": ...}}; use all_rows({side.target.__name__}) '
                f"to allow every {side.target.__name__}"
            )
        ftype = schema.specs()[side.ids_name].ftype
        self.check_link_scope(side)
        key = side.target.__field_types__[side.target.__primary_key__]
        if (
            ftype.collection != "set"
            or ftype.item is None
            or (ftype.item.kind != key.kind)
        ):
            raise RegistrationError(
                f"{name}: {schema.__name__}.{side.ids_name} must be a set of "
                f"{side.target.__name__} keys"
            )
        bound = ftype.info.max_length
        if schema is not self.read and (bound is None or bound > side.max_items):
            raise RegistrationError(
                f"{name}: {schema.__name__}.{side.ids_name} needs "
                f"Field(max_length=...) of at most {side.max_items}"
            )

    def check_link_scope(self, side: LinkEnd) -> None:
        """An ``own_rows`` or ``RowScope`` entry names a target column and needs
        access on every active action."""
        row_scope = self.link_scopes.get(side.declared)
        if row_scope is not None:
            self._check_row_link_scope(side, row_scope)
            return
        scope = self.links.get(side.declared)
        if isinstance(scope, OwnedRows):
            self._check_owned_link_scope(side, scope)

    def _check_owned_link_scope(self, side: LinkEnd, scope: OwnedRows[Any]) -> None:
        name = self.model.__name__
        kind = side.target.__field_types__.get(scope.column)
        if scope.model is not side.target or kind is None or kind.kind != "str":
            raise RegistrationError(
                f"crud({name}): own_rows({scope.model.__name__}, {scope.column!r}) "
                f"must name a text column of {side.target.__name__}"
            )
        if self._unprotected_link_actions():
            raise RegistrationError(
                f"crud({name}): own_rows needs access= on every action"
            )

    def _check_row_link_scope(self, side: LinkEnd, scope: RowScope[Any, Any]) -> None:
        name = self.model.__name__
        if scope.model is not side.target:
            raise RegistrationError(
                f"crud({name}): links[{side.declared!r}] RowScope belongs to "
                f"{scope.model.__name__}, not {side.target.__name__}"
            )
        if self._unprotected_link_actions():
            raise RegistrationError(
                f"crud({name}): RowScope in links= needs access= on every action"
            )

    def _unprotected_link_actions(self) -> bool:
        used = ["list", "retrieve"]
        used += [a for a, s in (("create", self.create), ("update", self.update)) if s]
        return any(self.access_for(a) is None for a in used if a not in self.omit)

    def check_filters(self) -> None:
        if self.filters is None:
            return
        if "list" in self.omit:
            raise RegistrationError(
                f"{self.model.__name__}: filters=... apply to the list action, "
                "which is omitted"
            )
        by_cursor = isinstance(self.pagination, CursorPagination)
        self.filters.check(self.model, cursor=by_cursor, reserved=self.pagination.names)

    def check_schemas(self, columns: set[str], *, custom_output: bool) -> None:
        if self.read.__derived_model__ not in (None, self.model):
            raise RegistrationError(
                f"{self.model.__name__}: read_schema is derived from "
                f"{self.read.__derived_model__.__name__}"
            )
        names = columns | set(self.link_ids)
        if self.create is not None:
            self.check_derived_model("create", self.create)
            self.check_input("create", self.create, names)
            self.check_required(self.create)
        if self.update is not None:
            self.check_derived_model("update", self.update)
            self.check_input("update", self.update, names)
            self.check_patch(self.update)
        if not custom_output:
            self.check_names("read", self.read, names)

    def check_derived_model(self, label: str, schema: type[Schema]) -> None:
        if schema.__derived_model__ not in (None, self.model):
            raise RegistrationError(
                f"{self.model.__name__}: {label}_schema is derived from "
                f"{schema.__derived_model__.__name__}"
            )

    def check_names(self, label: str, schema: type[Schema], columns: set[str]) -> None:
        unknown = set(schema.specs()) - columns
        if unknown:
            raise RegistrationError(
                f"{self.model.__name__}: {label} schema has fields the model "
                f"does not: {sorted(unknown)}"
            )

    def check_input(self, label: str, schema: type[Schema], columns: set[str]) -> None:
        self.check_names(label, schema, columns)
        self.check_nullable(label, schema)
        sent = set(schema.specs()) & self.server_columns()
        if sent:
            raise RegistrationError(
                f"{self.model.__name__}: {label} schema accepts {sorted(sent)}, "
                "which the server assigns"
            )

    def check_patch(self, update: type[Schema]) -> None:
        """PATCH touches only what was sent: every field defaults to UNSET and
        the primary key is not changeable."""
        key = self.model.__primary_key__
        if key in update.specs():
            raise RegistrationError(
                f"{self.model.__name__}: the update schema must not contain {key!r}"
            )
        loud = [
            item.name
            for item in dataclasses.fields(update)  # type: ignore[arg-type]
            if item.default is not UNSET
        ]
        if loud:
            raise RegistrationError(
                f"{self.model.__name__}: update schema fields {loud} must default "
                "to UNSET, or an absent field would overwrite the column"
            )

    def check_nullable(self, label: str, schema: type[Schema]) -> None:
        hints = typing.get_type_hints(self.model)
        for name, spec in schema.specs().items():
            optional = type(None) in typing.get_args(hints.get(name))
            if spec.ftype.nullable and not optional:
                raise RegistrationError(
                    f"{self.model.__name__}: {label} field {name!r} accepts null "
                    "but the column does not"
                )

    def check_required(self, create: type[Schema]) -> None:
        sent = {
            item.name
            for item in dataclasses.fields(create)  # type: ignore[arg-type]
            if item.name in create.specs() and item.default is not UNSET
        }
        covered = sent | self.server_columns()
        missing = [
            f.name
            for f in dataclasses.fields(self.model)  # type: ignore[arg-type]
            if f.default is dataclasses.MISSING
            and f.default_factory is dataclasses.MISSING
            and f.name not in covered
        ]
        if missing:
            raise RegistrationError(
                f"{self.model.__name__}: no way to set required columns {missing}"
            )

    def values(self, data: Schema) -> dict[str, Any]:
        found = {name: getattr(data, name) for name in data.specs()}
        return {
            name: value
            for name, value in found.items()
            if value is not UNSET and name not in self.link_ids
        }

    def owner_of(self, principal: Any) -> Any:
        """The caller's owner value; a caller without one may change nothing,
        or ``None`` would match every ownerless row."""
        value = (
            principal.id
            if self.identity_key is caller_id
            else self.identity_key(principal.user)
        )
        if value is None:
            raise Forbidden("You cannot change this item.")
        return value

    def scope(self, key: Any, principal: Any) -> QuerySet[Any]:
        """The rows a change may touch: the base query, the primary key and the
        owner, all in the UPDATE/DELETE itself, so a row that was hidden or
        changed hands meanwhile is untouched."""
        scoped = self.model.all().filter(**{self.model.__primary_key__: key})
        if self.owner:
            scoped = scoped.filter(**{self.owner: self.owner_of(principal)})
        return scoped

    async def fetch(self, key: Any) -> Any:
        return await self.model.aget(**{self.model.__primary_key__: key})

    async def written(self, key: Any) -> Any:
        """Read back a row the caller just wrote. It may now be outside the
        base query (archived, unpublished); the writer still sees the result."""
        try:
            return await self.fetch(key)
        except self.model.NotFound:
            return await self.model.aget(**{self.model.__primary_key__: key})

    async def guard(self, key: Any, principal: Any) -> None:
        """404 for a row outside the base query (or missing), 403 when somebody
        else owns it. Rows the base query hides look the same as missing ones."""
        row = await self.fetch(key)
        if self.owner and getattr(row, self.owner) != self.owner_of(principal):
            raise Forbidden("You cannot change this item.")


def check_filter_dialect_support(
    model: type[Model], filters: FilterSet, dialect: str
) -> None:
    """SQLite keeps a Decimal column as text: only equality (and ``.null``)
    filters compare it, and it cannot order a list. A client filter or
    ``ordering`` that needs more fails with a named 422 ``unsupported``
    detail instead of a database error at compile time."""
    if dialect != "sqlite":
        return
    for column, op, _ in filters.conditions:
        if op not in TEXT_DECIMAL_OPERATORS and decimal_columns(model, column):
            raise ValidationError(
                [ErrorDetail(column, "unsupported", "not supported on SQLite")]
            )
    for name in filters.order:
        column = name.removeprefix("-")
        if decimal_columns(model, column):
            raise ValidationError(
                [
                    ErrorDetail(
                        "ordering", "unsupported", "ordering is not supported on SQLite"
                    )
                ]
            )


def _scoped_query(self: Any, query: Any, principal: Any, *, hook: str) -> Any:
    """Validate hook ``QuerySet`` model and enforce ``resource.row_scope``."""
    row_scope = self.resource.row_scope
    if row_scope is None:
        return query
    if not isinstance(query, QuerySet) or query.model is not self.resource.model:
        raise RegistrationError(
            f"{type(self).__name__}.{hook} must return a "
            f"{self.resource.model.__name__} QuerySet"
        )
    return row_scope.apply(query, principal)


async def narrowed(
    self: CrudBase, query: QuerySet[Any], filters: FilterSet | None, principal: Any
) -> QuerySet[Any]:
    """What the client asked for, on top of ``get_queryset``: its filters (ANDed,
    never wider), then ``filter_queryset``, then its ordering."""
    if filters is None:
        return query
    check_filter_dialect_support(
        self.resource.model, filters, current_database().dialect
    )
    narrower = await self.call_hook(
        "filter_queryset", filters.apply(query), filters, principal
    )
    narrower = _scoped_query(self, narrower, principal, hook="filter_queryset")
    return filters.arrange(narrower)


async def list_rows(
    self: CrudBase,
    request: Request,
    page: PageParams,
    filters: FilterSet | None = None,
    principal: Any = None,
    _row_plan: RowWireSelection | None = None,
) -> Any:
    base = _scoped_query(
        self,
        await self.call_hook("get_queryset", principal),
        principal,
        hook="get_queryset",
    )
    query = await narrowed(self, base, filters, principal)
    if not query.order:
        query = query.order_by(self.resource.model.__primary_key__)
    query = await with_links(self, query, principal)
    if (
        _row_plan is not None
        and _row_plan.armed_for(self)
        and _row_plan.valid_query(base)
    ):
        return await wire_list_rows(self, query, page, _row_plan)
    return await python_list_rows(self, query, page)


async def python_list_rows(
    self: CrudBase, query: QuerySet[Any], page: PageParams
) -> Any:
    result = await query.apaginate(page)
    items = await schema_items(self, result.items)
    return Page(list(items), result.page, result.page_size, result.total)


async def wire_list_rows(
    self: CrudBase,
    query: QuerySet[Any],
    page: PageParams,
    selection: RowWireSelection,
) -> Any:
    window = query.limit(page.page_size).offset(page.offset)
    expected_order = query.order
    if not selection.valid_window(
        window,
        order=expected_order,
        limit=page.page_size,
        offset=page.offset,
    ):
        result = await query.apaginate(page)
        items = await schema_items(self, result.items)
        return Page(list(items), result.page, result.page_size, result.total)
    total = await query.acount()
    database = current_database()
    sql, params = window.compile(dialect=database.dialect)
    fetched = await database._fetch_page_wire(
        sql, params, selection.plan, page.page, page.page_size, total
    )
    if isinstance(fetched, bytes):
        return Response(200, fetched, content_type="application/json")
    models = await window._hydrate(fetched)
    items = await schema_items(self, models)
    return Page(list(items), page.page, page.page_size, total)


async def list_cursor_rows(
    self: CrudBase,
    request: Request,
    page: CursorParams,
    filters: FilterSet | None = None,
    principal: Any = None,
    _row_plan: RowWireSelection | None = None,
) -> Any:
    """The keyset list: the same ``get_queryset`` (ownership, tenant) as the
    offset list, the client's filters on top of it, and the cursor bound to
    those filters and to the ordering."""
    base = _scoped_query(
        self,
        await self.call_hook("get_queryset", principal),
        principal,
        hook="get_queryset",
    )
    query = await narrowed(self, base, filters, principal)
    query = await with_links(self, query, principal)
    cursor = prepare(query, page, b"" if filters is None else filters.scope)
    if (
        _row_plan is not None
        and _row_plan.armed_for(self)
        and _row_plan.valid_query(base)
        and _row_plan.valid_window(
            cursor.walk, order=cursor.walk.order, limit=page.limit + 1, offset=None
        )
    ):
        return await wire_cursor_rows(self, cursor, _row_plan)
    result = await fetch_request(cursor)
    items = await schema_items(self, result.items)
    return CursorPage(list(items), result.next, result.previous, result.page_size)


async def wire_cursor_rows(
    self: CrudBase, cursor: CursorRequest, selection: RowWireSelection
) -> Any:
    database = current_database()
    sql, params = cursor.compile(database.dialect)
    fetched = await database._fetch_cursor_wire(
        sql,
        params,
        selection.plan,
        cursor.codec,
        cursor.keys,
        cursor.limit,
        cursor.mode,
    )
    if isinstance(fetched, bytes):
        return Response(200, fetched, content_type="application/json")
    rows, more = fetched
    models = await cursor.walk._hydrate(rows)
    after, before = cursor.links(models, more)
    items = await schema_items(self, models)
    return CursorPage(list(items), after, before, cursor.limit)


async def schema_items(self: CrudBase, rows: list[Any]) -> list[Any]:
    if inspect.iscoroutinefunction(self.to_schema):
        items = await asyncio.gather(
            *(self.call_hook("to_schema", row) for row in rows)
        )
    else:
        items = [await self.call_hook("to_schema", row) for row in rows]
    return list(items)


def _scoped_link_query(self: Any, side: LinkEnd, query: Any, principal: Any) -> Any:
    """Validate link hook ``QuerySet`` model and enforce declared ``RowScope``."""
    row_scope = self.resource.link_scopes.get(side.declared)
    if row_scope is None:
        return query
    if not isinstance(query, QuerySet) or query.model is not side.target:
        raise RegistrationError(
            f"{type(self).__name__}.link_queryset({side.declared!r}) must return a "
            f"{side.target.__name__} QuerySet"
        )
    if (
        not isinstance(self, CrudBase)
        or type(self).link_queryset is not CrudBase.link_queryset
    ):
        return row_scope.apply(query, principal)
    return query


async def with_links(
    self: CrudBase, query: QuerySet[Any], principal: Any
) -> QuerySet[Any]:
    """Load the links the read schema shows, limited to the targets the caller
    may see; the serializer then reads them without I/O."""
    sides = self.resource.link_fields(self.resource.read)
    if not sides:
        return query
    scopes = {
        side.declared: _scoped_link_query(
            self,
            side,
            await self.call_hook("link_queryset", side.declared, principal),
            principal,
        )
        for side in sides
    }
    return query.prefetch_related(**scopes)


async def write_links(
    self: CrudBase,
    row: Any,
    schema: type[Schema] | None,
    data: Any,
    principal: Any,
    owner_within: QuerySet[Any] | None = None,
) -> None:
    """Replace the links the request sent. Targets outside the caller's scope
    are refused like missing ones; links the caller cannot see stay as they
    are. ``owner_within`` is re-checked once the owner row is locked: a row that
    left it meanwhile is refused. The route's transaction rolls everything back
    on an error."""
    for side in self.resource.link_fields(schema):
        ids = getattr(data, side.ids_name)
        if ids is UNSET:
            continue
        scope = _scoped_link_query(
            self,
            side,
            await self.call_hook("link_queryset", side.declared, principal),
            principal,
        )
        try:
            await getattr(row, side.declared).write(
                "set", tuple(ids), scope, owner_within
            )
        except OwnerOutOfScope:
            key = getattr(row, self.resource.model.__primary_key__)
            raise await self.refuse(key, principal) from None


async def retrieve_row(
    self: CrudBase, request: Request, item_id: Any, principal: Any = None
) -> Any:
    resource = self.resource
    query = _scoped_query(
        self,
        await self.call_hook("get_queryset", principal),
        principal,
        hook="get_queryset",
    )
    query = await with_links(self, query, principal)
    item = await query.filter(**{resource.model.__primary_key__: item_id}).aget()
    return await self.call_hook("to_schema", item)


async def create_row(
    self: CrudBase, request: Request, data: Any, principal: Any = None
) -> Any:
    resource = self.resource
    values = resource.values(data)
    assigned_values = resource.assign_values(principal)
    values.update(assigned_values)
    created = await self.call_hook("store", values, principal)
    self.check_written(created)
    if any(getattr(created, key) != value for key, value in assigned_values.items()):
        raise RegistrationError("store changed a server-assigned field")
    await write_links(self, created, resource.create, data, principal)
    key = getattr(created, created.__primary_key__)
    item = await self.reload_written(key, principal)
    if any(getattr(item, name) != value for name, value in assigned_values.items()):
        raise RegistrationError("store changed a server-assigned field")
    return await self.call_hook("to_schema", item)


async def update_row(
    self: CrudBase, request: Request, item_id: Any, data: Any, principal: Any = None
) -> Any:
    resource = self.resource
    query = _scoped_query(
        self,
        await self.call_hook("get_queryset", principal),
        principal,
        hook="get_queryset",
    )
    try:
        visible = await query.filter(**{resource.model.__primary_key__: item_id}).aget()
    except resource.model.NotFound:
        raise
    if resource.owner and getattr(visible, resource.owner) != resource.owner_of(
        principal
    ):
        raise Forbidden("You cannot change this item.")
    values = resource.values(data)
    scoped = query.filter(**{resource.model.__primary_key__: item_id})
    if resource.owner:
        scoped = scoped.filter(**{resource.owner: resource.owner_of(principal)})
    item = visible
    assigned_before = {name: getattr(item, name) for name in resource.server_columns()}
    updated = await self.call_hook("change", item, scoped, values, principal)
    self.check_written(updated)
    if getattr(updated, resource.model.__primary_key__) != item_id:
        raise RegistrationError("change returned a different item")
    if any(getattr(updated, name) != value for name, value in assigned_before.items()):
        raise RegistrationError("change changed a server-assigned field")
    await write_links(self, updated, resource.update, data, principal, scoped)
    persisted = await self.reload_written(item_id, principal)
    self.check_written(persisted)
    if any(
        getattr(persisted, name) != value for name, value in assigned_before.items()
    ):
        raise RegistrationError("change changed a server-assigned field")
    return await self.call_hook("to_schema", persisted)


async def delete_row(
    self: CrudBase, request: Request, item_id: Any, principal: Any = None
) -> None:
    resource = self.resource
    query = _scoped_query(
        self,
        await self.call_hook("get_queryset", principal),
        principal,
        hook="get_queryset",
    )
    visible = await query.filter(**{resource.model.__primary_key__: item_id}).aget()
    if resource.owner and getattr(visible, resource.owner) != resource.owner_of(
        principal
    ):
        raise Forbidden("You cannot change this item.")
    scoped = query.filter(**{resource.model.__primary_key__: item_id})
    if resource.owner:
        scoped = scoped.filter(**{resource.owner: resource.owner_of(principal)})
    item = visible
    if not await self.call_hook("remove", item, scoped, principal):
        raise await self.refuse(item_id, principal)


def specialize(
    template: Callable[..., Any], hints: dict[str, Any], name: str
) -> Callable[..., Any]:
    """Copy ``template`` with concrete annotations, so binding and OpenAPI see
    this resource's schemas. Parameters without a hint are left out of the
    signature (the template accepts them as optional)."""
    copy = types.FunctionType(
        template.__code__,
        template.__globals__,
        name,
        template.__defaults__,
        template.__closure__,
    )
    copy.__qualname__ = name
    copy.__annotations__ = hints
    signature = inspect.signature(template)
    parameters = [
        p.replace(annotation=hints.get(p.name, p.annotation))
        for p in signature.parameters.values()
        if p.name in hints or p.name == "self"
    ]
    copy.__signature__ = signature.replace(  # type: ignore[attr-defined]
        parameters=parameters, return_annotation=hints.get("return")
    )
    return copy


def _missing_handwritten_access(
    cls: type[CrudBase], name: str, member: object
) -> str | None:
    if getattr(member, GENERATED, False):
        return None
    route = route_of(member)
    if route is not None and not route.access_explicit:
        return (
            f"{cls.__name__}.{name}: handwritten action on a protected @crud "
            "resource must declare access=... (or access=None to make it public)"
        )
    ws = websocket_of(member)
    if ws is not None and not ws.access_explicit:
        return (
            f"{cls.__name__}.{name}: handwritten WebSocket action on a protected "
            "@crud resource must declare access=... (or access=None to make it public)"
        )
    return None


def check_handwritten_access(cls: type[CrudBase], config: _CrudConfig) -> None:
    """Require explicit ``access=`` on handwritten actions of a protected @crud."""
    if not config.is_protected():
        return
    for name in cls.member_names():
        message = _missing_handwritten_access(
            cls, name, inspect.getattr_static(cls, name)
        )
        if message is not None:
            raise RegistrationError(message)


class CrudBase(API):
    """The API class that ``crud(...)`` generates; it is not declared directly."""

    model: ClassVar[type[Model]]
    resource: ClassVar[_CrudConfig]

    def __init_subclass__(
        cls,
        path: str | None = None,
        resource: _CrudConfig | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init_subclass__(path=path, **kwargs)
        config = resource or next(
            (
                vars(base)["resource"]
                for base in cls.__mro__[1:]
                if "resource" in vars(base)
            ),
            None,
        )
        if config is None:
            return
        custom_output = cls.to_schema is not CrudBase.to_schema
        config.check(
            custom_output=custom_output,
            custom_links=cls.link_queryset is not CrudBase.link_queryset,
        )
        check_hook_callers(cls, config)
        warn_unscoped_owner_reads(cls, config)
        cls.resource = config
        cls.model = config.model
        generated = generate(config)
        for name in ACTIONS:
            existing = inspect.getattr_static(cls, name, None)
            if not getattr(existing, GENERATED, False) and existing is not None:
                continue  # handwritten: keep it
            # Inherited generated actions belong to the parent's resource (its
            # access, schemas and owner); a resource that does not declare them
            # must not keep them running against the new one.
            setattr(cls, name, generated.get(name))
        check_handwritten_access(cls, config)

    @classmethod
    def routes(cls) -> list[Route]:
        if hasattr(cls, "resource"):
            check_handwritten_access(cls, cls.resource)
        return super().routes()

    @classmethod
    def websockets(cls) -> list[WebSocketRoute]:
        if hasattr(cls, "resource"):
            check_handwritten_access(cls, cls.resource)
        return super().websockets()

    def get_queryset(
        self,
        principal: Caller[Any] | None,  # noqa: ARG002
    ) -> QuerySet[Any] | Awaitable[QuerySet[Any]]:
        """Build the visible query; public routes receive ``None``."""
        return self.resource.model.all()

    def filter_queryset(
        self,
        query: QuerySet[Any],
        params: Any,  # noqa: ARG002
        principal: Caller[Any] | None,  # noqa: ARG002
    ) -> QuerySet[Any] | Awaitable[QuerySet[Any]]:
        """Narrow the list further, after the client's filters; ``params`` is the
        ``FilterSet`` instance declared by ``crud(filters=...)``."""
        return query

    def written_queryset(
        self,
        principal: Caller[Any] | None,  # noqa: ARG002
    ) -> QuerySet[Any] | Awaitable[QuerySet[Any]]:
        """Build a query for reading back a successful write."""
        return self.resource.model.all()

    def scoped_query(
        self, query: Any, principal: Caller[Any] | None, *, hook: str
    ) -> QuerySet[Any]:
        """Verify hook return model and enforce ``resource.row_scope``."""
        return cast(QuerySet[Any], _scoped_query(self, query, principal, hook=hook))

    async def reload_written(self, item_id: Any, principal: Any) -> Any:
        """Read back a write through ``written_queryset``."""
        key = self.resource.model.__primary_key__
        query = _scoped_query(
            self,
            await self.call_hook("written_queryset", principal),
            principal,
            hook="written_queryset",
        )
        query = await with_links(self, query, principal)
        return await query.filter(**{key: item_id}).aget()

    def link_queryset(
        self,
        relation: str,
        principal: Caller[Any] | None,
    ) -> QuerySet[Any] | Awaitable[QuerySet[Any]]:
        """The targets of ``relation`` the caller may link and see: the
        ``links=`` entry applied to the caller's user (``None`` on a public
        route)."""
        row_scope = self.resource.link_scopes.get(relation)
        if row_scope is not None:
            return row_scope.for_caller(principal)
        scope = self.resource.links[relation]
        if isinstance(scope, OwnedRows):
            if principal is None or principal.id is None:
                raise Forbidden("You cannot change this item.")
            return scope(principal.id)
        user = None if principal is None else principal.user
        return scope(user)

    def to_schema(self, item: Any) -> Schema | Awaitable[Schema]:
        read = self.resource.read
        values = {name: getattr(item, name) for name in read.specs()}
        for name in self.resource.read_links:
            values[name] = set(values[name])
        try:
            return read(**values)
        except ValidationError as error:
            # The row came from the database, not from the client: a server fault.
            problems = ", ".join(f"{d.path} ({d.code})" for d in error.details)
            raise RuntimeError(
                f"a stored {self.resource.model.__name__} row does not satisfy "
                f"{read.__name__}: {problems}"
            ) from None

    def check_written(self, item: Any) -> None:
        if not isinstance(item, self.resource.model):
            raise RegistrationError(
                f"CRUD hook must return {self.resource.model.__name__}, got "
                f"{type(item).__name__}"
            )

    async def refuse(self, item_id: Any, principal: Any) -> Forbidden:
        """Preserve hidden-row 404s and owner-change 403s after a race."""
        resource = self.resource
        query = _scoped_query(
            self,
            await self.call_hook("get_queryset", principal),
            principal,
            hook="get_queryset",
        )
        item = await query.filter(**{resource.model.__primary_key__: item_id}).aget()
        if resource.owner and getattr(item, resource.owner) != resource.owner_of(
            principal
        ):
            return Forbidden("You cannot change this item.")
        return Forbidden("The item could not be changed.")

    def store(
        self,
        values: dict[str, Any],
        principal: Any,  # noqa: ARG002
    ) -> Model | Awaitable[Model]:
        return self.resource.model.acreate(**values)

    def change(
        self, item: Any, scoped: QuerySet[Any], values: dict[str, Any], principal: Any
    ) -> Any | Awaitable[Any]:
        return self.apply_change(item, scoped, values, principal)

    async def apply_change(
        self, item: Any, scoped: QuerySet[Any], values: dict[str, Any], principal: Any
    ) -> Any:
        if item.has_save_effects():
            # Hooks and events run, and the UPDATE carries the scope, so a row
            # that left it since it was read is refused, as on the unhooked path.
            try:
                return await item.aupdate_within(scoped, **values)
            except item.NotFound:
                raise await self.refuse(
                    getattr(item, item.__primary_key__), principal
                ) from None
        if values and not await scoped.aupdate(values):
            raise await self.refuse(getattr(item, item.__primary_key__), principal)
        return item

    def remove(
        self,
        item: Any,  # noqa: ARG002
        scoped: QuerySet[Any],
        principal: Any,  # noqa: ARG002
    ) -> bool | Awaitable[bool]:
        return self.apply_removal(scoped)

    async def apply_removal(self, scoped: QuerySet[Any]) -> bool:
        return bool(await scoped.adelete())

    async def call_hook(self, name: str, *arguments: Any) -> Any:
        """Run overrides as async, or on the bounded executor when sync."""
        if getattr(type(self), name) is getattr(CrudBase, name):
            return await self.call_default_hook(name, *arguments)
        return await self.call_custom_hook(name, *arguments)

    async def call_default_hook(self, name: str, *arguments: Any) -> Any:
        result = getattr(self, name)(*arguments)
        if name not in {
            "get_queryset",
            "filter_queryset",
            "written_queryset",
            "link_queryset",
        } and inspect.isawaitable(result):
            return await result
        return result

    async def call_custom_hook(self, name: str, *arguments: Any) -> Any:
        hook = getattr(self, name)
        if inspect.iscoroutinefunction(hook):
            return await hook(*arguments)
        runner = context.sync_runner.get()
        if runner is None:
            raise RuntimeError("a sync CRUD hook needs a running Hinoki request")
        result = await runner(hook, *arguments)
        if inspect.isawaitable(result) and not isinstance(result, QuerySet):
            return await result
        return result


def item_type(model: type[Model]) -> Any:
    return typing.get_type_hints(model)[model.__primary_key__]


def declare(
    template: Callable[..., Any],
    name: str,
    hints: dict[str, Any],
    route: tuple[str, str, Options],
) -> Callable[..., Any]:
    method, path, options = route
    function = RouteDeclaration(method, path, options)(
        specialize(template, hints, name)
    )
    setattr(function, GENERATED, True)
    return function


def check_cursor_order(resource: _CrudConfig) -> None:
    """Fail at registration when the model's own ordering cannot carry a cursor;
    a custom ``get_queryset`` ordering is still checked on each request."""
    try:
        cursor_order(resource.model.all())
    except FieldError as exc:
        raise RegistrationError(
            f"{resource.model.__name__}: list cannot use CursorPagination: {exc}"
        ) from exc


def read_routes(resource: _CrudConfig, key: Any) -> dict[str, Callable[..., Any]]:
    by_cursor = isinstance(resource.pagination, CursorPagination)
    if by_cursor:
        check_cursor_order(resource)
    page = (CursorPage if by_cursor else Page)[resource.read]
    template = list_cursor_rows if by_cursor else list_rows
    listed = Options(
        access=resource.access_for("list"),
        cors=resource.cors,
        **resource.documentation("list"),
    )
    listing = dataclasses.replace(listed, pagination=resource.pagination)
    shown = Options(
        access=resource.access_for("retrieve"),
        cors=resource.cors,
        **resource.documentation("retrieve"),
    )
    return {
        "list": declare(
            template,
            "list",
            {
                "request": Request,
                "page": CursorParams if by_cursor else PageParams,
                **({"filters": resource.filters} if resource.filters else {}),
                **caller_hint(resource, "list"),
                "return": page,
            },
            ("GET", "/", listing),
        ),
        "retrieve": declare(
            retrieve_row,
            "retrieve",
            {
                "request": Request,
                "item_id": key,
                **caller_hint(resource, "retrieve"),
                "return": resource.read,
            },
            ("GET", ITEM, shown),
        ),
    }


def caller_hint(resource: _CrudConfig, action: str) -> dict[str, Any]:
    return {"principal": Caller[Any]} if resource.access_for(action) else {}


def write_routes(resource: _CrudConfig, key: Any) -> dict[str, Callable[..., Any]]:
    def writes(action: str, status: int | None = None) -> Options:
        options = Options(
            access=resource.access_for(action),
            atomic=True,
            cors=resource.cors,
            **resource.documentation(action),
        )
        return dataclasses.replace(options, status=status) if status else options

    found: dict[str, Callable[..., Any]] = {}
    if resource.create:
        hints = {"request": Request, "data": resource.create}
        hints |= caller_hint(resource, "create") | {"return": resource.read}
        route = ("POST", "/", writes("create", 201))
        found["create"] = declare(create_row, "create", hints, route)
    if resource.update:
        hints = {"request": Request, "item_id": key, "data": resource.update}
        hints |= caller_hint(resource, "update") | {"return": resource.read}
        found["update"] = declare(
            update_row, "update", hints, ("PATCH", ITEM, writes("update"))
        )
    if resource.deletable:
        hints = {"request": Request, "item_id": key}
        hints |= caller_hint(resource, "delete") | {"return": type(None)}
        route = ("DELETE", ITEM, writes("delete", 204))
        found["delete"] = declare(delete_row, "delete", hints, route)
    return found


def generate(resource: _CrudConfig) -> dict[str, Callable[..., Any]]:
    """The generated actions of ``resource``, by name."""
    key = item_type(resource.model)
    found = {**read_routes(resource, key), **write_routes(resource, key)}
    return {name: route for name, route in found.items() if name not in resource.omit}


@dataclasses.dataclass(frozen=True, kw_only=True)
class CrudAccess:
    """Per-action access for ``crud(access=...)``.

    An action without its own entry uses ``default``; ``None`` means public.
    """

    default: Access | None = None
    list: Access | Unset | None = UNSET
    retrieve: Access | Unset | None = UNSET
    create: Access | Unset | None = UNSET
    update: Access | Unset | None = UNSET
    delete: Access | Unset | None = UNSET

    def resolve(self) -> dict[str, Access | None]:
        found = {name: getattr(self, name) for name in ACTIONS}
        return {
            name: self.default if isinstance(value, Unset) else value
            for name, value in found.items()
        }


def enabled(config: _CrudConfig, action: str) -> bool:
    """Whether ``config`` generates ``action`` (reads always, writes by schema)."""
    return {
        "create": config.create is not None,
        "update": config.update is not None,
        "delete": config.deletable,
    }.get(action, True)


def rebind_class_cells(old: type, new: type) -> None:
    """Point zero-argument ``super()`` and ``__class__`` in the methods at the
    class that was built, not the decorated one that is discarded."""
    for value in vars(old).values():
        parts = (
            (value.fget, value.fset, value.fdel)
            if isinstance(value, property)
            else (value,)
        )
        for part in parts:
            function = getattr(part, "__func__", part)
            for cell in getattr(function, "__closure__", None) or ():
                with contextlib.suppress(ValueError):  # an empty cell
                    if cell.cell_contents is old:
                        cell.cell_contents = new


class CrudDeclaration:
    """What ``crud(...)`` returns: include it for plain CRUD, or apply it to a
    class that adds hooks and handwritten actions."""

    def __init__(
        self, config: _CrudConfig, path: str, module: str = __name__, name: str = ""
    ) -> None:
        self.config = config
        self.path = path
        prefix, _, short = name.rpartition(".")
        self.module = prefix or module
        self.name = short or f"{config.model.__name__}Crud"
        self.named = bool(name)
        self.generated = [
            name
            for name in ACTIONS
            if name not in config.omit and enabled(config, name)
        ]
        self.built: type[CrudBase] | None = None

    def api(self) -> type[CrudBase]:
        """The generated API class (built once), named after the module that
        declared it (or ``name=``) so rules scoped to that module see its actions."""
        if self.built is None:
            self.built = self.build(self.name, {"__module__": self.module})
        return self.built

    def __call__[T: API](self, cls: type[T]) -> type[T]:
        if self.named:
            raise RegistrationError(
                f"{cls.__name__}: name= is for the function form; "
                "the decorated class keeps its own name"
            )
        if cls.__bases__ != (API,):
            raise RegistrationError(
                f"{cls.__name__}: a crud(...) class has exactly one base, API"
            )
        names = set(vars(cls))
        both = sorted(names & set(self.generated))
        if both:
            raise RegistrationError(
                f"{cls.__name__}: {both} are both generated and handwritten; "
                "omit=... the generated action or rename the handwritten one"
            )
        extra = sorted(names & set(INTERNAL_HOOKS))
        if extra:
            raise RegistrationError(
                f"{cls.__name__}: {extra} are internal helpers, not hooks; "
                f"the hooks are {', '.join(HOOKS)}"
            )
        skip = {"__dict__", "__weakref__"}
        built = self.build(
            cls.__name__, {k: v for k, v in vars(cls).items() if k not in skip}
        )
        rebind_class_cells(cls, built)
        return cast(type[T], built)

    def build(self, name: str, namespace: dict[str, Any]) -> type[CrudBase]:
        return cast(
            type[CrudBase],
            types.new_class(
                name,
                (CrudBase,),
                {"path": self.path, "resource": self.config},
                lambda body: body.update(namespace),
            ),
        )


def crud(  # noqa: PLR0913 - each keyword is an independent CRUD option
    model: type[Model],
    *,
    path: str,
    read: type[Schema],
    create: type[Schema] | None = None,
    update: type[Schema] | None = None,
    delete: bool = False,
    access: Access | CrudAccess | None = None,
    public_writes: bool = False,
    owner: str = "",
    identity_key: Callable[[Any], Any] = attrgetter("id"),
    assign: Mapping[str, Callable[[Any], Any]] | None = None,
    pagination: Pagination = PageNumberPagination(),  # noqa: B008
    filters: type[FilterSet] | None = None,
    omit: tuple[str, ...] = (),
    name: str | None = None,
    cors: Cors | None = None,
    include_in_schema: bool | Mapping[str, bool] = True,
    tags: Sequence[str] = (),
    deprecated: bool | date = False,
    scope: RowScope[Any, Any] | None = None,
    links: Mapping[str, Callable[[Any], QuerySet[Any]] | RowScope[Any, Any]]
    | None = None,
) -> CrudDeclaration:
    """Declare generated CRUD for ``model``.

    ``app.include(crud(...))`` mounts the generated actions; ``@crud(...)`` on a
    class adds the hooks (``get_queryset``, ``to_schema``, ``written_queryset``,
    ``store``, ``change``, ``remove``) and handwritten actions.
    A single ``Access`` protects every action; ``CrudAccess`` sets them apart.
    ``name=`` names the generated class (``"ArchiveCrud"``) or its module and
    class (``"notes.apps.archive.ArchiveCrud"``) that rules and MCP patterns match.
    ``cors=http.Cors(...)`` gives every generated action that CORS policy instead
    of the application's. ``filters=`` takes a ``FilterSet`` subclass: the list then
    accepts the filters, ordering and search it declares.
    ``include_in_schema=False`` leaves every generated action out of the OpenAPI
    document; ``include_in_schema={"delete": False}`` leaves out
    only the named actions. ``tags`` and ``deprecated`` apply to every action.
    Left-out actions are still served and protected by their access.
    ``links={"tags": all_rows(Tag)}`` scopes the targets a many-to-many ids field
    (``tag_ids``) may link and show: a function of the caller's user that returns
    a ``QuerySet`` of the target. Every ids field needs an entry.
    """
    unknown = sorted(set(omit) - {"list", "retrieve"})
    if unknown:
        raise RegistrationError(
            f"crud(...) omit= takes 'list' or 'retrieve', not {unknown}"
        )
    if name is not None:
        check_name(name)
    if filters is not None and not (
        isinstance(filters, type) and issubclass(filters, FilterSet)
    ):
        raise RegistrationError("crud(...) filters= takes a FilterSet subclass")
    if scope is not None and not isinstance(scope, RowScope):
        raise RegistrationError("crud(...) scope= takes a RowScope")
    try:
        check_tags(tags)
        check_deprecated(deprecated)
        shown = schema_visibility(include_in_schema)
    except ValueError as exc:
        raise RegistrationError(f"crud(...) {exc}") from exc
    actions = (
        access.resolve()
        if isinstance(access, CrudAccess)
        else dict.fromkeys(ACTIONS, access)
    )
    callable_links = {
        key: value
        for key, value in (links or {}).items()
        if not isinstance(value, RowScope)
    }
    row_link_scopes = {
        key: value
        for key, value in (links or {}).items()
        if isinstance(value, RowScope)
    }
    config = _CrudConfig(
        model=model,
        read=read,
        create=create,
        update=update,
        deletable=delete,
        access=actions["create"],
        read_access=actions["list"],
        public_writes=public_writes,
        owner=owner,
        identity_key=identity_key,
        assign=assign or {},
        pagination=pagination,
        filters=filters,
        actions=actions,
        omit=frozenset(omit),
        cors=cors,
        schema_shown=shown,
        tags=check_tags(tags),
        deprecated=deprecated,
        row_scope=scope,
        links=callable_links,
        link_scopes=row_link_scopes,
    )
    return CrudDeclaration(config, path, declaring_module(), name or "")


@dataclasses.dataclass(frozen=True, slots=True)
class AllRows[T: Model]:
    """A ``links=`` entry that lets the caller link every row of ``model``."""

    model: type[T]

    def __call__(self, user: object) -> QuerySet[T]:  # noqa: ARG002 - the scope ignores the caller
        return self.model.all()


@dataclasses.dataclass(frozen=True, slots=True)
class OwnedRows[T: Model]:
    """A ``links=`` entry that lets the caller link and see the rows of
    ``model`` whose ``column`` holds the caller's id."""

    model: type[T]
    column: str

    def __call__(self, owner: object) -> QuerySet[T]:
        return self.model.filter(**{self.column: owner})


def all_rows[T: Model](model: type[T]) -> Callable[[object], QuerySet[T]]:
    """A ``links=`` entry that lets the caller link every row of ``model``: the
    explicit choice for targets that are not private to anyone."""
    return AllRows(model)


def own_rows[T: Model](model: type[T], column: str) -> Callable[[object], QuerySet[T]]:
    """A ``links=`` entry that lets the caller link and see only the rows of
    ``model`` whose text ``column`` holds the caller's id (``Caller.id``). It
    needs access on every action, and generated CRUD keeps it on the native
    lane."""
    return OwnedRows(model, column)


def schema_visibility(value: bool | Mapping[str, bool]) -> dict[str, bool]:
    """``include_in_schema`` of ``crud(...)`` as one flag per generated action."""
    if isinstance(value, Mapping):
        unknown = sorted(set(value) - set(ACTIONS))
        if unknown:
            raise ValueError(f"include_in_schema= names unknown actions {unknown}")
        return {a: check_flag("include_in_schema", value.get(a, True)) for a in ACTIONS}
    return dict.fromkeys(ACTIONS, check_flag("include_in_schema", value))


def check_name(name: str) -> None:
    if not all(part.isidentifier() for part in name.split(".")):
        raise RegistrationError(
            f"crud(...) name={name!r} must be a class name or module.Class"
        )


def declaring_module() -> str:
    """The module that declared the ``crud(...)`` value: the caller's module, or,
    when the caller is a helper function run from a module's top level, that
    module (one hop, so a shared factory names actions after the feature)."""
    frame = inspect.currentframe()
    caller = frame.f_back.f_back if frame and frame.f_back else None
    if caller is None:
        return __name__  # pragma: no cover - a frame always has a caller here
    outer = caller.f_back
    if (
        caller.f_code.co_name != "<module>"
        and outer is not None
        and outer.f_code.co_name == "<module>"
    ):
        caller = outer
    return str(caller.f_globals.get("__name__", __name__))
