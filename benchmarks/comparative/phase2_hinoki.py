"""Handwritten-handler fixture for the Rust request-path (phase 2) baseline.

Every handler does the least possible work so the measurement is the request path
around it: routing, input binding, validation, the handler call and the response.
Only ``/items-tx`` touches the database, and only to open and commit an empty
transaction.
"""

from typing import Annotated

from hinoki import Field, Hinoki, Schema, Unset, http, settings
from hinoki.db import DatabaseModule
from hinoki.fields import UNSET


class ItemIn(Schema):
    title: Annotated[str, Field(min_length=1, max_length=120)]
    body: Annotated[str, Field(min_length=1)]
    count: Annotated[int, Field(ge=0)] = 0


class ItemPatch(Schema):
    title: str | Unset = UNSET
    count: int | Unset = UNSET


class Phase2Settings(settings.AppSettings, prefix="PHASE2_"):
    database: settings.DatabaseSettings


@http.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@http.get("/echo-str/{item_id}")
async def echo_str(item_id: str, view: str) -> dict[str, str]:
    return {"id": item_id, "view": view}


@http.get("/echo/{item_id}")
async def echo(
    item_id: int, limit: int = 20, ratio: float = 1.0, flag: bool = False
) -> dict[str, object]:
    return {"id": item_id, "limit": limit, "ratio": ratio, "flag": flag}


@http.post("/items", status=201)
async def create(data: ItemIn) -> dict[str, object]:
    return {"title": data.title, "size": len(data.body), "count": data.count}


@http.patch("/items/{item_id}")
async def patch(item_id: int, data: ItemPatch) -> dict[str, object]:
    changed = [name for name in ("title", "count") if getattr(data, name) is not UNSET]
    return {"id": item_id, "changed": changed}


@http.delete("/items/{item_id}", status=204)
async def delete(item_id: int) -> None:
    return None


@http.post("/items-tx", status=201, transaction=True)
async def create_in_transaction(data: ItemIn) -> dict[str, object]:
    return {"title": data.title, "size": len(data.body), "count": data.count}


app = Hinoki(
    settings=Phase2Settings,
    include=[health, echo_str, echo, create, patch, delete, create_in_transaction],
    modules=[DatabaseModule],
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)
