"""The benchmark service on Django Ninja (ASGI, Django async ORM)."""

import math
import os
import uuid
from datetime import datetime

import django
from django.core.asgi import get_asgi_application
from django.urls import path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dj_settings")
django.setup()

from benchapp.models import Author, Post  # noqa: E402
from ninja import Field, NinjaAPI, Query, Schema  # noqa: E402
from ninja.errors import HttpError  # noqa: E402


class AuthorOut(Schema):
    id: uuid.UUID
    name: str


class PostOut(Schema):
    id: uuid.UUID
    title: str
    body: str
    published: bool
    created_at: datetime
    author: AuthorOut


class PostIn(Schema):
    author_id: uuid.UUID
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1)


class PageOut(Schema):
    items: list[PostOut]
    page: int
    page_size: int
    total: int
    pages: int


class PageQuery(Schema):
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=100)


api = NinjaAPI(openapi_url=None, docs_url=None)


@api.get("/health")
async def health(request):
    return {"status": "ok"}


@api.get("/posts", response=PageOut)
async def listing(request, q: PageQuery = Query(...)):  # noqa: B008 - Ninja's declaration style
    published = Post.objects.filter(published=True)
    total = await published.acount()
    start = (q.page - 1) * q.page_size
    rows = [
        p
        async for p in published.select_related("author").order_by(
            "-created_at", "-id"
        )[start : start + q.page_size]
    ]
    return {
        "items": rows,
        "page": q.page,
        "page_size": q.page_size,
        "total": total,
        "pages": math.ceil(total / q.page_size),
    }


@api.get("/posts/{post_id}", response=PostOut)
async def one(request, post_id: uuid.UUID):
    try:
        return await Post.objects.select_related("author").aget(
            id=post_id, published=True
        )
    except Post.DoesNotExist:
        raise HttpError(404, "not found") from None


@api.post("/posts", response={201: PostOut})
async def create(request, data: PostIn):
    from django.utils import timezone

    post = await Post.objects.acreate(
        author_id=data.author_id,
        title=data.title,
        body=data.body,
        published=True,
        created_at=timezone.now(),
    )
    post.author = await Author.objects.aget(id=post.author_id)
    return 201, post


urlpatterns = [path("", api.urls)]
application = get_asgi_application()
