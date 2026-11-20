"""Class-based CRUD generation, composable with ordinary API methods."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import enum
import inspect
import types
import typing
from collections.abc import Awaitable, Callable, Mapping
from operator import attrgetter
from typing import Any, ClassVar, NoReturn, cast

from hinoki import context
from hinoki.access import Access
from hinoki.api import API
from hinoki.auth import Caller
from hinoki.errors import Forbidden, RegistrationError, ValidationError
from hinoki.fields import UNSET, Unset
from hinoki.http.request import Request
from hinoki.http.response import Response
from hinoki.orm import Model, QuerySet
from hinoki.orm.context import current_database
from hinoki.pagination import Page, PageNumberPagination, PageParams
from hinoki.routing import Options, RouteDeclaration
from hinoki.schema import Schema

if typing.TYPE_CHECKING:
    from hinoki.runtime.rowplan import RowWireSelection

ITEM = "/{item_id}"
GENERATED = "hinoki_generated"
ACTIONS = ("list", "retrieve", "create", "update", "delete")
WRITE_ACTIONS = ("create", "update", "delete")
HOOKS = (
    "get_queryset",
    "to_schema",
    "written_queryset",
    "store",
    "change",
    "remove",
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
    raise TypeError("caller_id is read from the Caller, not called")


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
    pagination: PageNumberPagination = PageNumberPagination()  # noqa: RUF009
    actions: Mapping[str, Access | None] | None = None
    omit: frozenset[str] = frozenset()

    def access_for(self, action: str) -> Access | None:
        """The policy of one generated action: its own entry when ``crud(...)``
        declared per-action access, else the write or read policy."""
        if self.actions is not None:
            return self.actions[action]
        return self.access if action in WRITE_ACTIONS else self.read_access

    def unprotected_writes(self) -> bool:
        return any(self.access_for(a) is None for a in self.writes() or ["create"])

    def assigned(self) -> dict[str, Callable[[Any], Any]]:
        extra = {self.owner: self.identity_key} if self.owner else {}
        return {**self.assign, **extra}

    def writes(self) -> list[str]:
        found = ["create"] if self.create else []
        found += ["update"] if self.update else []
        found += ["delete"] if self.deletable else []
        return found

    def check(self, *, custom_output: bool = False) -> None:
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
        self.check_schemas(columns, custom_output=custom_output)

    def check_schemas(self, columns: set[str], *, custom_output: bool) -> None:
        if self.read.__derived_model__ not in (None, self.model):
            raise RegistrationError(
                f"{self.model.__name__}: read_schema is derived from "
                f"{self.read.__derived_model__.__name__}"
            )
        if self.create is not None:
            self.check_derived_model("create", self.create)
            self.check_input("create", self.create, columns)
            self.check_required(self.create)
        if self.update is not None:
            self.check_derived_model("update", self.update)
            self.check_input("update", self.update, columns)
            self.check_patch(self.update)
        if not custom_output:
            self.check_names("read", self.read, columns)

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
        sent = set(schema.specs()) & set(self.assigned())
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
        covered = sent | set(self.assigned())
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
        return {name: value for name, value in found.items() if value is not UNSET}

    def present(self, row: Any) -> Any:
        return self.read(**{name: getattr(row, name) for name in self.read.specs()})

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

    async def refuse(self, key: Any) -> Forbidden:
        """A write matched no row: 404 if the row is gone or hidden by now,
        otherwise the owner changed under the caller."""
        await self.fetch(key)
        return Forbidden("You cannot change this item.")


async def list_rows(
    self: CrudBase,
    request: Request,
    page: PageParams,
    principal: Any = None,
    _row_plan: RowWireSelection | None = None,
) -> Any:
    if _row_plan is not None and _row_plan.valid_for(self):
        query = await self.call_hook("get_queryset", principal)
        if not _row_plan.valid_query(query):
            return await python_list_rows(self, query, page)
        if not query.order:
            query = query.order_by(self.resource.model.__primary_key__)
        return await wire_list_rows(self, query, page, _row_plan)
    query = await self.call_hook("get_queryset", principal)
    return await python_list_rows(self, query, page)


async def python_list_rows(
    self: CrudBase, query: QuerySet[Any], page: PageParams
) -> Any:
    resource = self.resource
    if not query.order:
        query = query.order_by(resource.model.__primary_key__)
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


async def schema_items(self: CrudBase, rows: list[Any]) -> list[Any]:
    if inspect.iscoroutinefunction(self.to_schema):
        items = await asyncio.gather(
            *(self.call_hook("to_schema", row) for row in rows)
        )
    else:
        items = [await self.call_hook("to_schema", row) for row in rows]
    return list(items)


async def retrieve_row(
    self: CrudBase, request: Request, item_id: Any, principal: Any = None
) -> Any:
    resource = self.resource
    query = await self.call_hook("get_queryset", principal)
    item = await query.filter(**{resource.model.__primary_key__: item_id}).aget()
    return await self.call_hook("to_schema", item)


async def create_row(
    self: CrudBase, request: Request, data: Any, principal: Any = None
) -> Any:
    resource = self.resource
    values = resource.values(data)
    assigned_values: dict[str, Any] = {}
    for column, derive in resource.assigned().items():
        assigned_values[column] = (
            resource.owner_of(principal)
            if column == resource.owner
            else derive(principal.user)
        )
    values.update(assigned_values)
    created = await self.call_hook("store", values, principal)
    self.check_written(created)
    if any(getattr(created, key) != value for key, value in assigned_values.items()):
        raise RegistrationError("store changed a server-assigned field")
    key = getattr(created, created.__primary_key__)
    item = await self.reload_written(key, principal)
    if any(getattr(item, name) != value for name, value in assigned_values.items()):
        raise RegistrationError("store changed a server-assigned field")
    return await self.call_hook("to_schema", item)


async def update_row(
    self: CrudBase, request: Request, item_id: Any, data: Any, principal: Any = None
) -> Any:
    resource = self.resource
    query = await self.call_hook("get_queryset", principal)
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
    assigned_before = {name: getattr(item, name) for name in resource.assigned()}
    updated = await self.call_hook("change", item, scoped, values, principal)
    self.check_written(updated)
    if getattr(updated, resource.model.__primary_key__) != item_id:
        raise RegistrationError("change returned a different item")
    if any(getattr(updated, name) != value for name, value in assigned_before.items()):
        raise RegistrationError("change changed a server-assigned field")
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
    query = await self.call_hook("get_queryset", principal)
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
        config.check(custom_output=custom_output)
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

    def get_queryset(
        self,
        principal: Caller[Any] | None,  # noqa: ARG002
    ) -> QuerySet[Any] | Awaitable[QuerySet[Any]]:
        """Build the visible query; public routes receive ``None``."""
        return self.resource.model.all()

    def written_queryset(
        self,
        principal: Caller[Any] | None,  # noqa: ARG002
    ) -> QuerySet[Any] | Awaitable[QuerySet[Any]]:
        """Build a query for reading back a successful write."""
        return self.resource.model.all()

    async def reload_written(self, item_id: Any, principal: Any) -> Any:
        """Read back a write through ``written_queryset``."""
        key = self.resource.model.__primary_key__
        query = await self.call_hook("written_queryset", principal)
        return await query.filter(**{key: item_id}).aget()

    def to_schema(self, item: Any) -> Schema | Awaitable[Schema]:
        read = self.resource.read
        try:
            return read(**{name: getattr(item, name) for name in read.specs()})
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
        query = await self.call_hook("get_queryset", principal)
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
        if item.write_hooks():
            # Hooks run, and the UPDATE carries the scope, so a row that left it
            # since it was read is refused, as on the unhooked path.
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
        if name not in {"get_queryset", "written_queryset"} and inspect.isawaitable(
            result
        ):
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


def read_routes(resource: _CrudConfig, key: Any) -> dict[str, Callable[..., Any]]:
    page = Page[resource.read]  # type: ignore[name-defined]
    listed = Options(access=resource.access_for("list"))
    listing = dataclasses.replace(listed, pagination=resource.pagination)
    shown = Options(access=resource.access_for("retrieve"))
    return {
        "list": declare(
            list_rows,
            "list",
            {
                "request": Request,
                "page": PageParams,
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
        options = Options(access=resource.access_for(action), atomic=True)
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

    def __init__(self, config: _CrudConfig, path: str) -> None:
        self.config = config
        self.path = path
        self.generated = [
            name
            for name in ACTIONS
            if name not in config.omit and enabled(config, name)
        ]
        self.built: type[CrudBase] | None = None

    def api(self) -> type[CrudBase]:
        """The generated API class (built once)."""
        if self.built is None:
            name = f"{self.config.model.__name__}Crud"
            self.built = self.build(name, {"__module__": __name__})
        return self.built

    def __call__[T: API](self, cls: type[T]) -> type[T]:
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
    pagination: PageNumberPagination = PageNumberPagination(),  # noqa: B008
    omit: tuple[str, ...] = (),
) -> CrudDeclaration:
    """Declare generated CRUD for ``model``.

    ``app.include(crud(...))`` mounts the generated actions; ``@crud(...)`` on a
    class adds the hooks (``get_queryset``, ``to_schema``, ``written_queryset``,
    ``store``, ``change``, ``remove``) and handwritten actions.
    A single ``Access`` protects every action; ``CrudAccess`` sets them apart.
    """
    unknown = sorted(set(omit) - {"list", "retrieve"})
    if unknown:
        raise RegistrationError(
            f"crud(...) omit= takes 'list' or 'retrieve', not {unknown}"
        )
    actions = (
        access.resolve()
        if isinstance(access, CrudAccess)
        else dict.fromkeys(ACTIONS, access)
    )
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
        actions=actions,
        omit=frozenset(omit),
    )
    return CrudDeclaration(config, path)
