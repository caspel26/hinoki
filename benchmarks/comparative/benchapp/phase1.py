"""Django Bolt endpoints equivalent to the generated CRUD orientation fixture."""

import math
import uuid

import msgspec
from django_bolt import BoltAPI
from django_bolt.exceptions import HTTPException

from benchapp.models import Phase1Post


class Phase1PostOut(msgspec.Struct):
    id: uuid.UUID
    title: str
    body: str


class Phase1PostCreate(msgspec.Struct):
    title: str
    body: str


class Phase1PostPatch(msgspec.Struct):
    title: str


class Phase1Page(msgspec.Struct):
    items: list[Phase1PostOut]
    page: int
    page_size: int
    total: int
    pages: int


def register_phase1(api: BoltAPI) -> None:
    """Register the phase 1 comparison routes on the benchmark API."""

    @api.get("/crud-posts")
    async def list_posts(page: int = 1, page_size: int = 20) -> Phase1Page:
        if page < 1 or not 1 <= page_size <= 100:
            raise HTTPException(status_code=422, detail="invalid page or page_size")
        posts = Phase1Post.objects.all()
        total = await posts.acount()
        start = (page - 1) * page_size
        items = [
            Phase1PostOut(id=row.id, title=row.title, body=row.body)
            async for row in posts.order_by("id")[start : start + page_size]
        ]
        return Phase1Page(items, page, page_size, total, math.ceil(total / page_size))

    @api.get("/crud-posts/{item_id}")
    async def retrieve_post(item_id: uuid.UUID) -> Phase1PostOut:
        try:
            row = await Phase1Post.objects.aget(id=item_id)
        except Phase1Post.DoesNotExist:
            raise HTTPException(status_code=404, detail="not found") from None
        return Phase1PostOut(id=row.id, title=row.title, body=row.body)

    @api.post("/crud-posts", status_code=201)
    async def create_post(data: Phase1PostCreate) -> Phase1PostOut:
        row = await Phase1Post.objects.acreate(title=data.title, body=data.body)
        return Phase1PostOut(id=row.id, title=row.title, body=row.body)

    @api.patch("/crud-posts/{item_id}")
    async def update_post(item_id: uuid.UUID, data: Phase1PostPatch) -> Phase1PostOut:
        changed = await Phase1Post.objects.filter(id=item_id).aupdate(title=data.title)
        if not changed:
            raise HTTPException(status_code=404, detail="not found")
        row = await Phase1Post.objects.aget(id=item_id)
        return Phase1PostOut(id=row.id, title=row.title, body=row.body)
