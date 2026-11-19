"""Sync (``def``) twins of the handlers in ``phase2_hinoki``.

The handlers do the same least-possible work, so the difference to the ``handlers``
suite is the cost of running a sync handler: the bounded executor hand-off.
There is no database scenario.
"""

from typing import Annotated

from hinoki import Field, Hinoki, Schema, Unset, http
from hinoki.fields import UNSET


class ItemIn(Schema):
    title: Annotated[str, Field(min_length=1, max_length=120)]
    body: Annotated[str, Field(min_length=1)]
    count: Annotated[int, Field(ge=0)] = 0


class ItemPatch(Schema):
    title: str | Unset = UNSET
    count: int | Unset = UNSET


@http.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@http.get("/echo-str/{item_id}")
def echo_str(item_id: str, view: str) -> dict[str, str]:
    return {"id": item_id, "view": view}


@http.get("/echo/{item_id}")
def echo(
    item_id: int, limit: int = 20, ratio: float = 1.0, flag: bool = False
) -> dict[str, object]:
    return {"id": item_id, "limit": limit, "ratio": ratio, "flag": flag}


@http.post("/items", status=201)
def create(data: ItemIn) -> dict[str, object]:
    return {"title": data.title, "size": len(data.body), "count": data.count}


@http.patch("/items/{item_id}")
def patch(item_id: int, data: ItemPatch) -> dict[str, object]:
    changed = [name for name in ("title", "count") if getattr(data, name) is not UNSET]
    return {"id": item_id, "changed": changed}


@http.delete("/items/{item_id}", status=204)
def delete(item_id: int) -> None:
    return None


app = Hinoki(
    include=[health, echo_str, echo, create, patch, delete],
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)
