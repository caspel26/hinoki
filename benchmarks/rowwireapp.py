"""Generated CRUD workload for the local row-wire A/B gate."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime
from typing import Annotated

from hinoki import CrudAPI, Field, Hinoki, Schema
from hinoki.db import DatabaseModule
from hinoki.orm import Model, field

DATABASE_URL = os.environ.get(
    "ROWWIRE_DATABASE_URL",
    "postgres://postgres:rowwire-only@127.0.0.1:55517/rowwire",
)
POOL_SIZE = int(os.environ.get("ROWWIRE_POOL_SIZE", "10"))


class RowRecord(Model, table="row_wire_bench_records"):
    id: int = field(primary_key=True)
    title: str
    count: int
    ratio: float
    active: bool
    created_at: datetime
    nullable: str | None
    hidden: int


class RowRecordOut(Schema):
    id: int
    title: Annotated[str, Field(alias="displayTitle")]
    count: int
    ratio: float
    active: bool
    created_at: datetime
    nullable: str | None


class RowRecords(CrudAPI, model=RowRecord, path="/records"):
    read_schema = RowRecordOut


def make_app(
    variant: str = "wire",
    *,
    database_url: str = DATABASE_URL,
    pool_size: int = POOL_SIZE,
) -> Hinoki:
    """Build the same generated route, disabling only its RowPlan for control."""
    if variant not in {"wire", "python"}:
        raise ValueError("variant must be 'wire' or 'python'")
    RowRecordOut.native_schema()
    app = Hinoki(
        include=[RowRecords],
        modules=[DatabaseModule(database_url, max_size=pool_size)],
    )
    list_binding = next(
        binding
        for binding in app.bindings
        if binding.route.path == "/records" and binding.route.handler.__name__ == "list"
    )
    if list_binding.row_wire is None:
        raise RuntimeError("generated list did not receive an eligible RowPlan")
    if variant == "python":
        index = app._bindings.index(list_binding)
        app._bindings[index] = replace(list_binding, row_wire=None)
    return app


app = make_app(os.environ.get("ROWWIRE_VARIANT", "wire"))
