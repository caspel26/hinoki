"""The benchmark service on Hinoki (ASGI, served by Granian)."""

from datetime import datetime
from typing import Annotated, ClassVar
from uuid import UUID, uuid4

from hinoki import Field, Hinoki, Schema, http, settings
from hinoki.db import DatabaseModule
from hinoki.orm import Model, field, foreign_key
from hinoki.orm.relations import Related
from hinoki.pagination import Page, PageNumberPagination, PageParams
from hinoki.utils import utc_now


class Author(Model, table="authors"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str = field(max_length=80)


class Post(Model, table="posts"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    author_id: UUID = foreign_key(Author, on_delete="restrict", related_name="posts")
    title: str = field(max_length=120)
    body: str
    published: bool = False
    created_at: datetime = field(default_factory=utc_now)
    author: ClassVar[Related[Author]]


class AuthorOut(Schema):
    id: UUID
    name: str


class PostOut(Schema):
    id: UUID
    title: str
    body: str
    published: bool
    created_at: datetime
    author: AuthorOut


class PostIn(Schema):
    author_id: UUID
    title: Annotated[str, Field(min_length=1, max_length=120)]
    body: Annotated[str, Field(min_length=1)]


class Health(Schema):
    status: str


class BenchSettings(settings.AppSettings, prefix="BENCH_"):
    database: settings.DatabaseSettings


def out(post: Post, author: Author) -> PostOut:
    return PostOut(
        id=post.id,
        title=post.title,
        body=post.body,
        published=post.published,
        created_at=post.created_at,
        author=AuthorOut(id=author.id, name=author.name),
    )


def with_author(post: Post) -> PostOut:
    return out(post, post.author)


@http.get("/health")
async def health() -> Health:
    return Health(status="ok")


@http.get("/posts", pagination=PageNumberPagination(page_size=20, max_page_size=100))
async def listing(page: PageParams) -> Page[PostOut]:
    found = (
        await Post.filter(published=True)
        .select_related("author")
        .order_by("-created_at", "-id")
        .apaginate(page)
    )
    return found.map(with_author)


@http.get("/posts/{post_id}")
async def one(post_id: UUID) -> PostOut:
    post = await Post.filter(id=post_id, published=True).select_related("author").aget()
    return with_author(post)


@http.post("/posts", status=201, transaction=True)
async def create(data: PostIn) -> PostOut:
    post = await Post.acreate(
        author_id=data.author_id, title=data.title, body=data.body, published=True
    )
    return out(post, await Author.aget(id=post.author_id))


app = Hinoki(
    settings=BenchSettings,
    include=[health, listing, one, create],
    modules=[DatabaseModule],
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)
