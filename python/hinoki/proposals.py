"""Starting-point source for a model, its schemas and CRUD actions.

Used by the development MCP tool ``propose_model``. Pure text generation from a
validated description: nothing is imported, written or run. The output follows
``docs/PATTERNS.md`` (separate In/Patch/Out schemas, pagination, transactions on
writes) and leaves the access policy as a visible TODO for the owner.
"""

from __future__ import annotations

import keyword
from typing import Any

TYPES = {
    "str": "str",
    "int": "int",
    "float": "float",
    "bool": "bool",
    "uuid": "UUID",
    "datetime": "datetime",
}
RESERVED = {"id", "data", "request", "self", "cls"}
SHADOWS = {
    "Model", "UUID", "Field", "Request", "Schema", "Unset", "UNSET", "Annotated",
    "field", "datetime", "uuid4", "annotated", "dump", "from_model", "to_dict",
}  # fmt: skip
MAX_NAME = 64
MAX_LENGTH = 1_000_000
MAX_FIELDS = 30


class ProposalError(ValueError):
    """The description cannot be turned into code; the message says why."""


def identifier(value: Any, what: str) -> str:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isidentifier()
        or keyword.iskeyword(value)
        or len(value) > MAX_NAME
        or value.startswith("_")
        or value in SHADOWS
    ):
        raise ProposalError(
            f"{what} must be a plain ASCII identifier (at most 64 characters, not private, not a name the generated code uses), got {value!r}"
        )
    return value


def flag(raw: dict[str, Any], key: str, name: str) -> bool:
    value = raw.get(key, False)
    if not isinstance(value, bool):
        raise ProposalError(f"field {name!r}: {key} is true or false")
    return value


class Column:
    def __init__(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            raise ProposalError("each field is an object with name and type")
        self.name = identifier(raw.get("name"), "field name")
        if self.name != self.name.lower() or self.name in RESERVED:
            raise ProposalError(
                f"field {self.name!r}: use lower_snake_case and not one of "
                f"{sorted(RESERVED)} (the id is added for you)"
            )
        kind = raw.get("type", "str")
        if not isinstance(kind, str) or kind not in TYPES:
            raise ProposalError(
                f"field {self.name!r}: type must be one of {sorted(TYPES)}"
            )
        self.kind = TYPES[kind]
        self.nullable = flag(raw, "nullable", self.name)
        self.unique = flag(raw, "unique", self.name)
        limit = raw.get("max_length")
        if limit is not None and (
            kind != "str"
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_LENGTH
        ):
            raise ProposalError(
                f"field {self.name!r}: max_length is a positive int on str"
            )
        self.limit: int | None = limit

    def model_line(self) -> str:
        options = [f"max_length={self.limit}"] if self.limit else []
        if self.unique:
            options.append("unique=True")
        annotation = f"{self.kind} | None" if self.nullable else self.kind
        if options or self.nullable:
            options += ["default=None"] if self.nullable else []
            return f"    {self.name}: {annotation} = field({', '.join(options)})"
        return f"    {self.name}: {annotation}"

    def schema_annotation(self, patch: bool) -> str:
        base = self.kind
        if self.kind == "str":
            parts = ["min_length=1"] + (
                [f"max_length={self.limit}"] if self.limit else []
            )
            base = f"Annotated[str, Field({', '.join(parts)})]"
        if self.nullable:
            base += " | None"
        return f"{base} | Unset = UNSET" if patch else base


def propose(name: Any, table: Any, fields: Any) -> dict[str, Any]:
    model = identifier(name, "name")
    if not model[0].isupper():
        raise ProposalError("name is the model class name, e.g. 'Article'")
    if not isinstance(fields, list) or not 1 <= len(fields) <= MAX_FIELDS:
        raise ProposalError(f"fields is a list of 1 to {MAX_FIELDS} objects")
    columns = [Column(item) for item in fields]
    if len({c.name for c in columns}) != len(columns):
        raise ProposalError("field names must be unique")
    table_name = identifier(table or model.lower() + "s", "table")
    path = "/" + table_name.replace("_", "-")
    required = [c for c in columns if not c.nullable]
    optional = [c for c in columns if c.nullable]
    lines = [
        "from typing import Annotated",
        "from uuid import UUID, uuid4",
        "from datetime import datetime  # noqa: F401 - only if a field uses it",
        "",
        "from hinoki import UNSET, Field, Request, Schema, Unset, delete, get, patch, post",
        "from hinoki.orm import Model, field",
        "",
        "",
        f'class {model}(Model, table="{table_name}"):',
        "    id: UUID = field(primary_key=True, default_factory=uuid4)",
        *(c.model_line() for c in columns),
        "",
        "",
        f"class {model}In(Schema):  # what a client may set",
        *(f"    {c.name}: {c.schema_annotation(False)}" for c in required),
        *(f"    {c.name}: {c.schema_annotation(False)} = None" for c in optional),
        "",
        "",
        f"class {model}Patch(Schema):  # every field optional: only what was sent",
        *(f"    {c.name}: {c.schema_annotation(True)}" for c in columns),
        "",
        "",
        f"class {model}Out(Schema):  # what a client may see",
        "    id: UUID",
        *(f"    {c.name}: {c.kind}{' | None' if c.nullable else ''}" for c in columns),
        "",
        "",
        f'@get("{path}")  # TODO: add access=Access(...) and pagination',
        f"async def list_{table_name}(request: Request) -> list[{model}Out]:",
        f"    return [{model}Out.from_model(row) for row in await {model}.all().aall()]",
        "",
        "",
        f'@get("{path}/{{item_id}}")',
        f"async def get_{model.lower()}(request: Request, item_id: UUID) -> {model}Out:",
        f"    return {model}Out.from_model(await {model}.aget(id=item_id))",
        "",
        "",
        f'@post("{path}", status=201, transaction=True)',
        f"async def create_{model.lower()}(request: Request, data: {model}In) -> {model}Out:",
        f"    return {model}Out.from_model(await {model}.acreate(data))",
        "",
        "",
        f'@patch("{path}/{{item_id}}", transaction=True)',
        f"async def update_{model.lower()}(",
        f"    request: Request, item_id: UUID, data: {model}Patch",
        f") -> {model}Out:",
        f"    row = await {model}.aget(id=item_id)",
        f"    return {model}Out.from_model(await row.aupdate_from(data))",
        "",
        "",
        f'@delete("{path}/{{item_id}}", status=204, transaction=True)',
        f"async def delete_{model.lower()}(request: Request, item_id: UUID) -> None:",
        f"    await {model}.adelete(id=item_id)",
        "",
    ]
    return {
        "source": "\n".join(lines),
        "next_steps": [
            "Review and edit; this is a starting point, not a finished design.",
            "Add Access(...) to every action that is not public on purpose "
            "(rule HNK-ACC-001) and pagination to the listing.",
            "Fields the server assigns (owner, state) must not be in the In schema.",
            f"Run: python -m hinoki migrations plan --models <module> --name add_{table_name}",
            "Run: python -m hinoki check",
        ],
    }
