"""The benchmark service on Hinoki (Granian, or Kiso with ``hinoki serve``).

The routes, response fields and SQL statements follow Django Bolt's comparison
app; ``parity_data.py`` explains what is generated here instead of copied.
"""

from datetime import datetime
from typing import ClassVar

import parity_data

from hinoki import Access, Caller, Hinoki, Schema, db, http, settings
from hinoki.auth import Credentials
from hinoki.crypto import JWTAuth, JWTKey
from hinoki.db import DatabaseModule
from hinoki.errors import Unauthorized
from hinoki.orm import (
    ManyToMany,
    Model,
    OnDelete,
    Related,
    field,
    foreign_key,
    many_to_many,
)
from hinoki.pagination import PageParams


class BenchmarkUser(Model, table="benchmark_users"):
    id: int = field(primary_key=True)
    username: str = field(max_length=50)
    email: str = field(max_length=254)
    first_name: str = field(max_length=50)
    last_name: str = field(max_length=50)
    is_active: bool = True


class User(Model, table="auth_user"):
    id: int = field(primary_key=True)
    username: str = field(max_length=150)
    email: str = field(max_length=254)
    first_name: str = field(max_length=150)
    last_name: str = field(max_length=150)
    is_active: bool = True


class Author(Model, table="articles_author"):
    id: int = field(primary_key=True)
    name: str = field(max_length=100)
    email: str = field(max_length=254)
    bio: str
    created_at: datetime


class Tag(Model, table="articles_tag"):
    id: int = field(primary_key=True)
    name: str = field(max_length=50)
    slug: str = field(max_length=50)


class Article(Model, table="articles_article"):
    id: int = field(primary_key=True)
    title: str = field(max_length=200)
    content: str
    published: bool = False
    author_id: int = foreign_key(
        Author, on_delete=OnDelete.RESTRICT, related_name="articles"
    )
    created_at: datetime
    updated_at: datetime
    author: ClassVar[Related[Author]]
    comments: ClassVar["Related[list[Comment]]"]
    tags: ClassVar[ManyToMany[Tag]] = many_to_many(
        Tag,
        related_name="articles",
        table="articles_article_tags",
        columns=("article_id", "tag_id"),
    )


@db.ordered_by("-created_at")
class Comment(Model, table="articles_comment"):
    id: int = field(primary_key=True)
    article_id: int = foreign_key(
        Article, on_delete=OnDelete.CASCADE, related_name="comments"
    )
    author_name: str = field(max_length=100)
    content: str
    created_at: datetime


class UserOut(Schema):
    id: int
    username: str
    email: str
    first_name: str
    last_name: str
    is_active: bool


class AuthorOut(Schema):
    id: int
    name: str
    email: str
    bio: str
    created_at: datetime


class TagOut(Schema):
    id: int
    name: str
    slug: str


class CommentOut(Schema):
    id: int
    article_id: int
    author_name: str
    content: str
    created_at: datetime


class ArticleOut(Schema):
    id: int
    title: str
    content: str
    published: bool
    author: AuthorOut
    tags: list[TagOut]
    created_at: datetime
    updated_at: datetime


class ArticleDetailOut(Schema):
    id: int
    title: str
    content: str
    published: bool
    author: AuthorOut
    tags: list[TagOut]
    comments: list[CommentOut]
    created_at: datetime
    updated_at: datetime


class ArticlePage(Schema):
    items: list[ArticleOut]
    total: int
    page: int
    page_size: int
    pages: int


class Health(Schema):
    status: str


class BenchSettings(settings.AppSettings, prefix="BENCH_"):
    database: settings.DatabaseSettings


def cookie(header: str, name: str) -> str | None:
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


class CookieTokens(JWTAuth[User]):
    """The token of the ``access_token`` cookie (Hinoki's ``JWTAuth`` reads a
    bearer header, so this provider reads the cookie and reuses its verifier)."""

    keys = (JWTKey(None, parity_data.ALGORITHM, parity_data.SECRET),)

    async def authenticate(self, credentials: Credentials) -> Caller[User] | None:
        token = cookie(credentials.headers.get("cookie", ""), parity_data.COOKIE)
        if token is None:
            return None
        caller = await self.resolve_token(token)
        if caller is None:
            raise Unauthorized("Invalid credentials.")
        return caller

    async def resolve_claims(self, claims: dict[str, object]) -> Caller[User] | None:
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.isdigit():
            return None
        user = await User.aget_or_none(id=int(subject))
        return None if user is None else Caller(subject, user)


signed_in = Access(auth=CookieTokens())


def author_out(author: Author) -> AuthorOut:
    return AuthorOut(
        id=author.id,
        name=author.name,
        email=author.email,
        bio=author.bio,
        created_at=author.created_at,
    )


def tags_out(article: Article) -> list[TagOut]:
    return [TagOut(id=t.id, name=t.name, slug=t.slug) for t in article.tags.loaded]


def article_out(article: Article) -> ArticleOut:
    return ArticleOut(
        id=article.id,
        title=article.title,
        content=article.content,
        published=article.published,
        author=author_out(article.author),
        tags=tags_out(article),
        created_at=article.created_at,
        updated_at=article.updated_at,
    )


async def articles_page(page: int, page_size: int) -> ArticlePage:
    params = PageParams(page=max(1, page), page_size=max(1, min(100, page_size)))
    paged = (
        await Article.all()
        .select_related("author")
        .prefetch_related("tags")
        .order_by("-created_at")
        .apaginate(params, concurrent=True)
    )
    return ArticlePage(
        items=[article_out(article) for article in paged.items],
        total=paged.total,
        page=paged.page,
        page_size=paged.page_size,
        pages=paged.pages,
    )


@http.get("/health")
async def health() -> Health:
    return Health(status="ok")


@http.get("/json-1k")
async def json_1k() -> list[dict[str, object]]:
    return parity_data.PAYLOADS["json-1k"]


@http.get("/json-10k")
async def json_10k() -> list[dict[str, object]]:
    return parity_data.PAYLOADS["json-10k"]


@http.get("/db")
async def db_read() -> list[UserOut]:
    return await BenchmarkUser.all().limit(10).aproject(UserOut)


@http.get("/articles")
async def list_articles(page: int = 1, page_size: int = 20) -> ArticlePage:
    return await articles_page(page, page_size)


@http.get("/articles/{article_id}")
async def get_article(article_id: int) -> ArticleDetailOut:
    article = (
        await Article.filter(id=article_id)
        .select_related("author")
        .prefetch_related("tags", "comments")
        .aget()
    )
    return ArticleDetailOut(
        id=article.id,
        title=article.title,
        content=article.content,
        published=article.published,
        author=author_out(article.author),
        tags=tags_out(article),
        comments=[
            CommentOut(
                id=c.id,
                article_id=c.article_id,
                author_name=c.author_name,
                content=c.content,
                created_at=c.created_at,
            )
            for c in article.comments
        ],
        created_at=article.created_at,
        updated_at=article.updated_at,
    )


@http.get("/auth/me", access=signed_in)
async def auth_me(caller: Caller[User]) -> UserOut:
    user = caller.user
    return UserOut(
        id=user.id,
        username=user.username,
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        is_active=user.is_active,
    )


@http.get("/auth/articles", access=signed_in)
async def auth_articles(page: int = 1, page_size: int = 20) -> ArticlePage:
    # The policy has already loaded the user row (one SELECT), as on every stack.
    return await articles_page(page, page_size)


app = Hinoki(
    settings=BenchSettings,
    include=[
        health,
        json_1k,
        json_10k,
        db_read,
        list_articles,
        get_article,
        auth_me,
        auth_articles,
    ],
    modules=[DatabaseModule],
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)
