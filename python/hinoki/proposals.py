"""Starting-point source for a model, its schemas and CRUD actions.

Used by the development MCP tool ``propose_model``. Pure text generation from a
validated description: nothing is imported, written or run. The output follows
``docs/patterns.md`` (separate In/Patch/Out schemas, pagination, transactions on
writes) and leaves the access policy as a visible TODO for the owner.
"""

from __future__ import annotations

import keyword
import re
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
PATH = r"/[a-z0-9-]+(/[a-z0-9-]+)*"


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


def groups_of(raw: Any, names: set[str]) -> list[list[str]]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(
        isinstance(g, list) and g and all(isinstance(n, str) for n in g) for g in raw
    ):
        raise ProposalError("unique_together is a list of lists of field names")
    for group in raw:
        unknown = [n for n in group if n not in names]
        if unknown:
            raise ProposalError(f"unique_together names unknown fields {unknown}")
    return raw


def names_of(
    table: Any, model: str, owner: Any, path: Any, columns: list[Column]
) -> tuple[str, str | None, str]:
    """The validated table name, owner model and URL path."""
    if "owner_id" in {c.name for c in columns}:
        raise ProposalError("owner_id is added when you pass owner; do not declare it")
    table_name = identifier(table or model.lower() + "s", "table")
    owner_model = identifier(owner, "owner") if owner is not None else None
    if owner_model is not None and not owner_model[0].isupper():
        raise ProposalError("owner is the owning model class name, e.g. 'Member'")
    if path is None:
        path = "/" + table_name.replace("_", "-")
    elif not isinstance(path, str) or not re.fullmatch(PATH, path):
        raise ProposalError("path looks like /bookmarks (lower-case, digits, dashes)")
    return table_name, owner_model, path


def propose(
    name: Any,
    table: Any,
    fields: Any,
    owner: Any = None,
    path: Any = None,
    unique_together: Any = None,
) -> dict[str, Any]:
    model = identifier(name, "name")
    if not model[0].isupper():
        raise ProposalError("name is the model class name, e.g. 'Article'")
    if not isinstance(fields, list) or not 1 <= len(fields) <= MAX_FIELDS:
        raise ProposalError(f"fields is a list of 1 to {MAX_FIELDS} objects")
    columns = [Column(item) for item in fields]
    if len({c.name for c in columns}) != len(columns):
        raise ProposalError("field names must be unique")
    table_name, owner_model, path = names_of(table, model, owner, path, columns)
    unique = groups_of(unique_together, {c.name for c in columns} | {"owner_id"})
    lower = model.lower()
    required = [c for c in columns if not c.nullable]
    optional = [c for c in columns if c.nullable]
    owned = owner_model is not None
    class_options = f'table="{table_name}"' + (f", unique={unique!r}" if unique else "")
    lines = [
        "from datetime import datetime  # noqa: F401 - only if a field uses it",
        "from typing import Annotated",
        "from uuid import UUID, uuid4",
        "",
        "from hinoki import UNSET, Caller, Field, Schema, Unset, http",
        "from hinoki.orm import Model, field" + (", foreign_key" if owned else ""),
        "from hinoki.pagination import Page, PageNumberPagination, PageParams",
        "",
        "",
        f"class {model}(Model, {class_options}):",
        "    id: UUID = field(primary_key=True, default_factory=uuid4)",
        *(
            [
                f'    owner_id: UUID = foreign_key({owner_model}, on_delete="restrict", '
                f'related_name="{table_name}")'
            ]
            if owned
            else []
        ),
        *(c.model_line() for c in columns),
        "",
        "",
        f"class {model}In(Schema):  # what a client may set"
        + ("; the owner is the caller" if owned else ""),
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
    ]
    guard = "access=member_only, " if owned else ""
    todo = "" if owned else "  # TODO: add access=Access(...) unless public on purpose"
    who = f", caller: Caller[{owner_model}]" if owned else ""
    scope = ", owner_id=caller.user.id" if owned else ""
    lines += [
        "PAGES = PageNumberPagination(page_size=10, max_page_size=50)",
        "",
        "",
        f'@http.get("{path}", {guard}pagination=PAGES){todo}',
        f"async def list_{table_name}(",
        f"    page: PageParams{who}",
        f") -> Page[{model}Out]:",
        f'    found = await {model}.filter({scope[2:]}).order_by("-id").apaginate(page)'
        if owned
        else f'    found = await {model}.all().order_by("-id").apaginate(page)',
        f"    return found.map({model}Out.from_model)",
        "",
        "",
        f'@http.get("{path}/{{item_id}}", {guard.rstrip(", ")}){todo}'.replace(
            ", )", ")"
        ),
        f"async def get_{lower}(item_id: UUID{who}) -> {model}Out:",
        f"    row = await {model}.aget(id=item_id{scope})",
        f"    return {model}Out.from_model(row)",
        "",
        "",
        f'@http.post("{path}", status=201, {guard}transaction=True){todo}',
        f"async def create_{lower}(",
        f"    data: {model}In{who}",
        f") -> {model}Out:",
        f"    row = await {model}.acreate(data{scope})",
        f"    return {model}Out.from_model(row)",
        "",
        "",
        f'@http.patch("{path}/{{item_id}}", {guard}transaction=True){todo}',
        f"async def update_{lower}(",
        f"    item_id: UUID, data: {model}Patch{who}",
        f") -> {model}Out:",
        f"    row = await {model}.aget(id=item_id{scope})",
        f"    return {model}Out.from_model(await row.aupdate_from(data))",
        "",
        "",
        f'@http.delete("{path}/{{item_id}}", status=204, {guard}transaction=True){todo}',
        f"async def delete_{lower}(item_id: UUID{who}) -> None:",
        f"    removed = await {model}.adelete(id=item_id{scope})",
        "    if not removed:",
        f"        raise {model}.NotFound()",
        "",
    ]
    steps = [
        "Review and edit; this is a starting point, not a finished design.",
        "Fields the server assigns (state, timestamps) must not be in the In schema.",
        f"Run: hinoki migrations plan --models <module> --name add_{table_name}",
        "Run: hinoki check",
    ]
    if owned:
        steps.insert(
            1,
            f"Define member_only = Access(auth=..., permissions=[...]) for {owner_model}; "
            "every row query is scoped by owner_id so another member's row looks missing.",
        )
    else:
        steps.insert(
            1,
            "No owner given: rows are not scoped. Add Access(...) and, for per-user data, "
            "regenerate with owner=<Model>.",
        )
    return {"source": "\n".join(lines), "next_steps": steps}
