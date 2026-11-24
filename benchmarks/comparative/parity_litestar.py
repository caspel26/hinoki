"""The benchmark service on Litestar (msgspec structs, SQLAlchemy, asyncpg).

Authentication is Litestar's ``JWTCookieAuth`` on the ``/auth`` router only, with
its own session to load the user, as Litestar's documentation shows.
"""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import msgspec
import parity_data
import parity_sqla
from litestar import Litestar, Request, Router, get
from litestar.connection import ASGIConnection
from litestar.di import Provide
from litestar.exceptions import NotFoundException
from litestar.security.jwt import JWTCookieAuth, Token
from sqlalchemy.ext.asyncio import AsyncSession


class UserOut(msgspec.Struct):
    id: int
    username: str
    email: str
    first_name: str
    last_name: str
    is_active: bool


class AuthorOut(msgspec.Struct):
    id: int
    name: str
    email: str
    bio: str
    created_at: datetime


class TagOut(msgspec.Struct):
    id: int
    name: str
    slug: str


class CommentOut(msgspec.Struct):
    id: int
    article_id: int
    author_name: str
    content: str
    created_at: datetime


class ArticleOut(msgspec.Struct):
    id: int
    title: str
    content: str
    published: bool
    author: AuthorOut
    tags: list[TagOut]
    created_at: datetime
    updated_at: datetime


class ArticleDetailOut(msgspec.Struct):
    id: int
    title: str
    content: str
    published: bool
    author: AuthorOut
    tags: list[TagOut]
    comments: list[CommentOut]
    created_at: datetime
    updated_at: datetime


class ArticlePage(msgspec.Struct):
    items: list[ArticleOut]
    total: int
    page: int
    page_size: int
    pages: int


async def session() -> AsyncIterator[AsyncSession]:
    async with parity_sqla.sessions() as s:
        yield s


def user_out(user: Any) -> UserOut:
    return UserOut(
        id=user.id,
        username=user.username,
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        is_active=user.is_active,
    )


def author_out(author: parity_sqla.Author) -> AuthorOut:
    return AuthorOut(
        id=author.id,
        name=author.name,
        email=author.email,
        bio=author.bio,
        created_at=author.created_at,
    )


def tags_out(article: parity_sqla.Article) -> list[TagOut]:
    return [TagOut(id=t.id, name=t.name, slug=t.slug) for t in article.tags]


def article_out(article: parity_sqla.Article) -> ArticleOut:
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


async def page_of(db: AsyncSession, page: int, page_size: int) -> ArticlePage:
    rows, total, page, page_size, pages = await parity_sqla.articles_page(
        db, page, page_size
    )
    return ArticlePage(
        items=[article_out(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
    )


@get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@get("/json-1k")
async def json_1k() -> list[dict[str, Any]]:
    return parity_data.PAYLOADS["json-1k"]


@get("/json-10k")
async def json_10k() -> list[dict[str, Any]]:
    return parity_data.PAYLOADS["json-10k"]


@get("/db")
async def db_read(db: AsyncSession) -> list[UserOut]:
    return [user_out(u) for u in await parity_sqla.benchmark_users(db)]


@get("/articles")
async def list_articles(
    db: AsyncSession, page: int = 1, page_size: int = 20
) -> ArticlePage:
    return await page_of(db, page, page_size)


@get("/articles/{article_id:int}")
async def get_article(db: AsyncSession, article_id: int) -> ArticleDetailOut:
    article = await parity_sqla.article_detail(db, article_id)
    if article is None:
        raise NotFoundException(detail="Article not found")
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


async def retrieve_user(
    token: Token, _connection: ASGIConnection[Any, Any, Any, Any]
) -> parity_sqla.AuthUser | None:
    if not token.sub.isdigit():
        return None
    async with parity_sqla.sessions() as db:
        return await db.get(parity_sqla.AuthUser, int(token.sub))


cookie_auth = JWTCookieAuth[parity_sqla.AuthUser](
    retrieve_user_handler=retrieve_user,
    token_secret=parity_data.SECRET,
    algorithm=parity_data.ALGORITHM,
    key=parity_data.COOKIE,
    secure=False,
)


@get("/me")
async def auth_me(request: Request[parity_sqla.AuthUser, Token, Any]) -> UserOut:
    return user_out(request.user)


@get("/articles")
async def auth_articles(
    db: AsyncSession, page: int = 1, page_size: int = 20
) -> ArticlePage:
    return await page_of(db, page, page_size)


app = Litestar(
    route_handlers=[
        health,
        json_1k,
        json_10k,
        db_read,
        list_articles,
        get_article,
        Router(
            path="/auth",
            route_handlers=[auth_me, auth_articles],
            middleware=[cookie_auth.middleware],
        ),
    ],
    dependencies={"db": Provide(session)},
    openapi_config=None,
    on_shutdown=[parity_sqla.engine.dispose],
)
