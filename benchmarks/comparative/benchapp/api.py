"""The benchmark service on Django Bolt (its own Rust HTTP server, Django async ORM)."""

import math
import uuid
from datetime import datetime

import msgspec
import payloads
from django.utils import timezone
from django_bolt import BoltAPI
from django_bolt.exceptions import HTTPException
from django_bolt.responses import Response

from benchapp.models import Author, Post
from benchapp.phase1 import register_phase1

api = BoltAPI()


class AuthorOut(msgspec.Struct):
    id: uuid.UUID
    name: str


class PostOut(msgspec.Struct):
    id: uuid.UUID
    title: str
    body: str
    published: bool
    created_at: datetime
    author: AuthorOut


class PostIn(msgspec.Struct):
    author_id: uuid.UUID
    title: str = msgspec.field()
    body: str = msgspec.field()


class PageOut(msgspec.Struct):
    items: list[PostOut]
    page: int
    page_size: int
    total: int
    pages: int


def out(p: Post) -> PostOut:
    return PostOut(
        id=p.id,
        title=p.title,
        body=p.body,
        published=p.published,
        created_at=p.created_at,
        author=AuthorOut(id=p.author.id, name=p.author.name),
    )


@api.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@api.get("/json-1k")
async def json_1k() -> Response:
    return Response(payloads.BODY_1K, media_type="application/json")


@api.get("/json-10k")
async def json_10k() -> Response:
    return Response(payloads.BODY_10K, media_type="application/json")


@api.get("/posts")
async def listing(page: int = 1, page_size: int = 20) -> PageOut:
    if page < 1 or not 1 <= page_size <= 100:
        raise HTTPException(status_code=422, detail="invalid page or page_size")
    published = Post.objects.filter(published=True)
    total = await published.acount()
    start = (page - 1) * page_size
    rows = [
        p
        async for p in published.select_related("author").order_by(
            "-created_at", "-id"
        )[start : start + page_size]
    ]
    return PageOut(
        items=[out(p) for p in rows],
        page=page,
        page_size=page_size,
        total=total,
        pages=math.ceil(total / page_size),
    )


@api.get("/posts/{post_id}")
async def one(post_id: uuid.UUID) -> PostOut:
    try:
        post = await Post.objects.select_related("author").aget(
            id=post_id, published=True
        )
    except Post.DoesNotExist:
        raise HTTPException(status_code=404, detail="not found") from None
    return out(post)


@api.post("/posts", status_code=201)
async def create(data: PostIn) -> PostOut:
    if not 1 <= len(data.title) <= 120 or not data.body:
        raise HTTPException(status_code=422, detail="invalid title or body")
    post = await Post.objects.acreate(
        author_id=data.author_id,
        title=data.title,
        body=data.body,
        published=True,
        created_at=timezone.now(),
    )
    post.author = await Author.objects.aget(id=post.author_id)
    return out(post)


register_phase1(api)
