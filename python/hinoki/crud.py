"""Generated CRUD actions for a model, as ordinary routes.

``class Posts(CrudAPI, path="/posts", resource=Resource(...))`` adds ``list``,
``retrieve`` and, depending on the resource, ``create``, ``update`` and
``delete`` to the class. They are real declarations: they appear in OpenAPI,
a method of the same name overrides one, and ``@http.get(..., replaces=Crud.LIST)``
(or the plain string ``"list"``) swaps one for a handwritten route. Generated
CRUD never invents security: write actions need an ``Access`` policy (or
``public_writes=True``), clients cannot send a field the server assigns, and an
owner column is enforced on change.
"""

from __future__ import annotations

import dataclasses
import enum
import inspect
import types
import typing
from collections.abc import Callable, Mapping
from operator import attrgetter
from typing import Any, ClassVar

from hinoki.access import Access
from hinoki.api import API
from hinoki.auth import Caller
from hinoki.errors import RegistrationError
from hinoki.fields import UNSET
from hinoki.httperrors import Forbidden
from hinoki.orm import Model, QuerySet
from hinoki.pagination import Page, PageNumberPagination, PageParams
from hinoki.request import Request
from hinoki.routing import Options, RouteDeclaration
from hinoki.schema import Schema

ITEM = "/{item_id}"
GENERATED = "hinoki_generated"
ACTIONS = ("list", "retrieve", "create", "update", "delete")


class Crud(enum.StrEnum):
    """The generated actions ``replaces=`` can name (each is also its plain string)."""

    LIST = "list"
    RETRIEVE = "retrieve"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


@dataclasses.dataclass(frozen=True, kw_only=True)
class Resource:
    """What to generate for ``model``.

    * ``read`` is the output schema; ``create`` / ``update`` (PATCH, fields left
      ``UNSET`` are untouched) enable those actions; ``deletable`` enables delete.
    * ``access`` protects the write actions, ``read_access`` the reads.
    * ``owner`` names a column that holds the owner: it is assigned from the
      caller on create and must match the caller on update and delete.
      ``identity_key`` extracts the owner value from ``Caller.user``.
    * ``assign`` maps other columns to a function of the caller's identity,
      applied on create; clients cannot send them.
    * ``queryset`` supplies the base query (e.g. with ``select_related``) and
      ``output`` converts a row to the read schema when it is not a plain copy.
    """

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
    queryset: Callable[[], QuerySet[Any]] | None = None
    output: Callable[[Any], Any] | None = None
    pagination: PageNumberPagination = PageNumberPagination()  # noqa: RUF009

    def assigned(self) -> dict[str, Callable[[Any], Any]]:
        extra = {self.owner: self.identity_key} if self.owner else {}
        return {**self.assign, **extra}

    def writes(self) -> list[str]:
        found = ["create"] if self.create else []
        found += ["update"] if self.update else []
        found += ["delete"] if self.deletable else []
        return found

    def check(self) -> None:
        """Fail at class creation, not on the first request."""
        name = self.model.__name__
        columns = set(self.model.__columns__)
        if self.writes() and self.access is None and not self.public_writes:
            raise RegistrationError(
                f"{name}: write actions need access=... (or public_writes=True)"
            )
        if self.assigned() and self.access is None:
            raise RegistrationError(f"{name}: owner/assign need an access policy")
        for column in self.assigned():
            if column not in columns:
                raise RegistrationError(f"{name} has no column {column!r} to assign")
        self.check_schemas(columns)

    def check_schemas(self, columns: set[str]) -> None:
        if self.create is not None:
            self.check_input("create", self.create, columns)
            self.check_required(self.create)
        if self.update is not None:
            self.check_input("update", self.update, columns)
            self.check_patch(self.update)
        if self.output is None:
            self.check_names("read", self.read, columns)

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

    def base(self) -> QuerySet[Any]:
        return self.queryset() if self.queryset else self.model.all()

    def present(self, row: Any) -> Any:
        if self.output is not None:
            return self.output(row)
        return self.read(**{name: getattr(row, name) for name in self.read.specs()})

    def values(self, data: Schema) -> dict[str, Any]:
        found = {name: getattr(data, name) for name in data.specs()}
        return {name: value for name, value in found.items() if value is not UNSET}

    def owner_of(self, principal: Any) -> Any:
        """The caller's owner value; a caller without one may change nothing,
        or ``None`` would match every ownerless row."""
        value = self.identity_key(principal.user)
        if value is None:
            raise Forbidden("You cannot change this item.")
        return value

    def scope(self, key: Any, principal: Any) -> QuerySet[Any]:
        """The rows a change may touch: the base query, the primary key and the
        owner, all in the UPDATE/DELETE itself, so a row that was hidden or
        changed hands meanwhile is untouched."""
        scoped = self.base().filter(**{self.model.__primary_key__: key})
        if self.owner:
            scoped = scoped.filter(**{self.owner: self.owner_of(principal)})
        return scoped

    async def fetch(self, key: Any) -> Any:
        return await self.base().filter(**{self.model.__primary_key__: key}).aget()

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

    async def refused(self, key: Any) -> Forbidden:
        """A write matched no row: 404 if the row is gone or hidden by now,
        otherwise the owner changed under the caller."""
        await self.fetch(key)
        return Forbidden("You cannot change this item.")


async def list_rows(self: CrudAPI, request: Request, page: PageParams) -> Any:
    resource = self.resource
    query = resource.base()
    if not query.order:
        query = query.order_by(resource.model.__primary_key__)
    return (await query.apaginate(page)).map(resource.present)


async def retrieve_row(self: CrudAPI, request: Request, item_id: Any) -> Any:
    return self.resource.present(await self.resource.fetch(item_id))


async def create_row(
    self: CrudAPI, request: Request, data: Any, principal: Any = None
) -> Any:
    resource = self.resource
    values = resource.values(data)
    for column, derive in resource.assigned().items():
        values[column] = (
            resource.owner_of(principal)
            if column == resource.owner
            else derive(principal.user)
        )
    created = await resource.model.acreate(**values)
    return resource.present(
        await resource.written(getattr(created, created.__primary_key__))
    )


async def update_row(
    self: CrudAPI, request: Request, item_id: Any, data: Any, principal: Any = None
) -> Any:
    resource = self.resource
    await resource.guard(item_id, principal)
    values = resource.values(data)
    if values and not await resource.scope(item_id, principal).aupdate(values):
        raise await resource.refused(item_id)
    return resource.present(await resource.written(item_id))


async def delete_row(
    self: CrudAPI, request: Request, item_id: Any, principal: Any = None
) -> None:
    resource = self.resource
    await resource.guard(item_id, principal)
    if not await resource.scope(item_id, principal).adelete():
        raise await resource.refused(item_id)


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


class CrudAPI(API):
    """Base class: ``class Posts(CrudAPI, path="/posts", resource=...)``."""

    resource: ClassVar[Resource]

    def __init_subclass__(
        cls, path: str | None = None, resource: Resource | None = None, **kwargs: Any
    ) -> None:
        super().__init_subclass__(path=path, **kwargs)
        if resource is None:
            return
        resource.check()
        cls.resource = resource
        generated = generate(resource)
        for name in ACTIONS:
            existing = inspect.getattr_static(cls, name, None)
            if not getattr(existing, GENERATED, False) and existing is not None:
                continue  # handwritten: keep it
            # Inherited generated actions belong to the parent's resource (its
            # access, schemas and owner); a resource that does not declare them
            # must not keep them running against the new one.
            setattr(cls, name, generated.get(name))


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


def read_routes(resource: Resource, key: Any) -> dict[str, Callable[..., Any]]:
    reads = Options(access=resource.read_access)
    listing = dataclasses.replace(reads, pagination=resource.pagination)
    page = Page[resource.read]  # type: ignore[name-defined]
    return {
        "list": declare(
            list_rows,
            "list",
            {"request": Request, "page": PageParams, "return": page},
            ("GET", "/", listing),
        ),
        "retrieve": declare(
            retrieve_row,
            "retrieve",
            {"request": Request, "item_id": key, "return": resource.read},
            ("GET", ITEM, reads),
        ),
    }


def write_routes(resource: Resource, key: Any) -> dict[str, Callable[..., Any]]:
    writes = Options(access=resource.access, transaction=True)
    caller = {"principal": Caller[Any]} if resource.access else {}
    found: dict[str, Callable[..., Any]] = {}
    if resource.create:
        hints = {"request": Request, "data": resource.create, **caller}
        hints["return"] = resource.read
        route = ("POST", "/", dataclasses.replace(writes, status=201))
        found["create"] = declare(create_row, "create", hints, route)
    if resource.update:
        hints = {"request": Request, "item_id": key, "data": resource.update, **caller}
        hints["return"] = resource.read
        found["update"] = declare(update_row, "update", hints, ("PATCH", ITEM, writes))
    if resource.deletable:
        hints = {"request": Request, "item_id": key, **caller, "return": type(None)}
        route = ("DELETE", ITEM, dataclasses.replace(writes, status=204))
        found["delete"] = declare(delete_row, "delete", hints, route)
    return found


def generate(resource: Resource) -> dict[str, Callable[..., Any]]:
    """The generated actions of ``resource``, by name."""
    key = item_type(resource.model)
    return {**read_routes(resource, key), **write_routes(resource, key)}
