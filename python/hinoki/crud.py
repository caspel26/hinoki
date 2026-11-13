"""Generated CRUD actions for a model, as ordinary routes.

``class Posts(CrudAPI, path="/posts", resource=Resource(...))`` adds ``list``,
``retrieve`` and, depending on the resource, ``create``, ``update`` and
``delete`` to the class. They are real declarations: they appear in OpenAPI,
a method of the same name overrides one, and ``@get(..., replaces="list")``
swaps one for a handwritten route. Generated CRUD never invents security: write
actions need an ``Access`` policy (or ``public_writes=True``), clients cannot
send a field the server assigns, and an owner column is enforced on change.
"""

from __future__ import annotations

import dataclasses
import inspect
import types
import typing
from collections.abc import Callable, Mapping
from operator import attrgetter
from typing import Any, ClassVar

from hinoki.access import Access
from hinoki.api import API
from hinoki.auth import Principal
from hinoki.errors import RegistrationError
from hinoki.fields import UNSET
from hinoki.httperrors import Forbidden
from hinoki.orm import Model, QuerySet
from hinoki.pagination import Page, PageNumberPagination, PageParams
from hinoki.routing import Options, RouteDeclaration
from hinoki.schema import Schema

ITEM = "/{item_id}"


@dataclasses.dataclass(frozen=True, kw_only=True)
class Resource:
    """What to generate for ``model``.

    * ``read`` is the output schema; ``create`` / ``update`` (PATCH, fields left
      ``UNSET`` are untouched) enable those actions; ``deletable`` enables delete.
    * ``access`` protects the write actions, ``read_access`` the reads.
    * ``owner`` names a column that holds the owner: it is assigned from the
      caller on create and must match the caller on update and delete.
      ``identity_key`` extracts the owner value from ``Principal.identity``.
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
        for label, schema in (("create", self.create), ("update", self.update)):
            if schema is not None:
                self.check_input(label, schema, columns)
        if self.output is None:
            self.check_names("read", self.read, columns)
        if self.create is not None:
            self.check_required(self.create)

    def check_names(self, label: str, schema: type[Schema], columns: set[str]) -> None:
        unknown = set(schema.specs()) - columns
        if unknown:
            raise RegistrationError(
                f"{self.model.__name__}: {label} schema has fields the model "
                f"does not: {sorted(unknown)}"
            )

    def check_input(self, label: str, schema: type[Schema], columns: set[str]) -> None:
        self.check_names(label, schema, columns)
        sent = set(schema.specs()) & set(self.assigned())
        if sent:
            raise RegistrationError(
                f"{self.model.__name__}: {label} schema accepts {sorted(sent)}, "
                "which the server assigns"
            )

    def check_required(self, create: type[Schema]) -> None:
        covered = set(create.specs()) | set(self.assigned())
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

    def owned(self, row: Any, principal: Any) -> None:
        if self.owner and getattr(row, self.owner) != self.identity_key(
            principal.identity
        ):
            raise Forbidden("You cannot change this item.")

    async def fetch(self, key: Any) -> Any:
        return await self.base().filter(**{self.model.__primary_key__: key}).aget()

    async def guard(self, key: Any, principal: Any) -> None:
        """404 for a missing row, 403 when somebody else owns it."""
        row = await self.model.aget(**{self.model.__primary_key__: key})
        self.owned(row, principal)


async def list_rows(self: CrudAPI, page: PageParams) -> Any:
    resource = self.resource
    ordered = resource.base().order_by(resource.model.__primary_key__)
    return (await ordered.apaginate(page)).map(resource.present)


async def retrieve_row(self: CrudAPI, item_id: Any) -> Any:
    return self.resource.present(await self.resource.fetch(item_id))


async def create_row(self: CrudAPI, data: Any, principal: Any = None) -> Any:
    resource = self.resource
    values = resource.values(data)
    for column, derive in resource.assigned().items():
        values[column] = derive(principal.identity)
    created = await resource.model.acreate(**values)
    return resource.present(
        await resource.fetch(getattr(created, created.__primary_key__))
    )


async def update_row(
    self: CrudAPI, item_id: Any, data: Any, principal: Any = None
) -> Any:
    resource = self.resource
    await resource.guard(item_id, principal)
    values = resource.values(data)
    if values:
        key = resource.model.__primary_key__
        await resource.model.aupdate(**{key: item_id}, **values)
    return resource.present(await resource.fetch(item_id))


async def delete_row(self: CrudAPI, item_id: Any, principal: Any = None) -> None:
    resource = self.resource
    await resource.guard(item_id, principal)
    await resource.model.adelete(**{resource.model.__primary_key__: item_id})


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
        for name, function in generate(resource).items():
            if name not in vars(cls):
                setattr(cls, name, function)


def item_type(model: type[Model]) -> Any:
    return typing.get_type_hints(model)[model.__primary_key__]


def declare(
    template: Callable[..., Any],
    name: str,
    hints: dict[str, Any],
    route: tuple[str, str, Options],
) -> Callable[..., Any]:
    method, path, options = route
    return RouteDeclaration(method, path, options)(specialize(template, hints, name))


def read_routes(resource: Resource, key: Any) -> dict[str, Callable[..., Any]]:
    reads = Options(access=resource.read_access)
    listing = dataclasses.replace(reads, pagination=resource.pagination)
    page = Page[resource.read]  # type: ignore[name-defined]
    return {
        "list": declare(
            list_rows,
            "list",
            {"page": PageParams, "return": page},
            ("GET", "/", listing),
        ),
        "retrieve": declare(
            retrieve_row,
            "retrieve",
            {"item_id": key, "return": resource.read},
            ("GET", ITEM, reads),
        ),
    }


def write_routes(resource: Resource, key: Any) -> dict[str, Callable[..., Any]]:
    writes = Options(access=resource.access, transaction=True)
    caller = {"principal": Principal[Any]} if resource.access else {}
    found: dict[str, Callable[..., Any]] = {}
    if resource.create:
        hints = {"data": resource.create, **caller, "return": resource.read}
        route = ("POST", "/", dataclasses.replace(writes, status=201))
        found["create"] = declare(create_row, "create", hints, route)
    if resource.update:
        hints = {"item_id": key, "data": resource.update, **caller}
        hints["return"] = resource.read
        found["update"] = declare(update_row, "update", hints, ("PATCH", ITEM, writes))
    if resource.deletable:
        hints = {"item_id": key, **caller, "return": type(None)}
        route = ("DELETE", ITEM, dataclasses.replace(writes, status=204))
        found["delete"] = declare(delete_row, "delete", hints, route)
    return found


def generate(resource: Resource) -> dict[str, Callable[..., Any]]:
    """The generated actions of ``resource``, by name."""
    key = item_type(resource.model)
    return {**read_routes(resource, key), **write_routes(resource, key)}
