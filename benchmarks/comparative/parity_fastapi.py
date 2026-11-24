"""The benchmark service on FastAPI (Pydantic models, SQLAlchemy, asyncpg)."""

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any

import parity_data
import parity_sqla
from fastapi import Cookie, Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserOut(Out):
    id: int
    username: str
    email: str
    first_name: str
    last_name: str
    is_active: bool


class AuthorOut(Out):
    id: int
    name: str
    email: str
    bio: str
    created_at: datetime


class TagOut(Out):
    id: int
    name: str
    slug: str


class CommentOut(Out):
    id: int
    article_id: int
    author_name: str
    content: str
    created_at: datetime


class ArticleOut(Out):
    id: int
    title: str
    content: str
    published: bool
    author: AuthorOut
    tags: list[TagOut]
    created_at: datetime
    updated_at: datetime


class ArticleDetailOut(Out):
    id: int
    title: str
    content: str
    published: bool
    author: AuthorOut
    tags: list[TagOut]
    comments: list[CommentOut]
    created_at: datetime
    updated_at: datetime


class ArticlePage(BaseModel):
    items: list[ArticleOut]
    total: int
    page: int
    page_size: int
    pages: int


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await parity_sqla.engine.dispose()


app = FastAPI(lifespan=lifespan, openapi_url=None, docs_url=None, redoc_url=None)


async def session():
    async with parity_sqla.sessions() as s:
        yield s


Db = Annotated[AsyncSession, Depends(session)]
Token = Annotated[str | None, Cookie(alias=parity_data.COOKIE)]


async def signed_in(db: Db, token: Token = None) -> parity_sqla.AuthUser:
    user = await parity_sqla.current_user(db, token)
    if user is None:
        raise HTTPException(401, "Invalid credentials")
    return user


async def page_of(db: AsyncSession, page: int, page_size: int) -> ArticlePage:
    rows, total, page, page_size, pages = await parity_sqla.articles_page(
        db, page, page_size
    )
    return ArticlePage(
        items=[ArticleOut.model_validate(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/json-1k")
async def json_1k() -> Any:
    return parity_data.PAYLOADS["json-1k"]


@app.get("/json-10k")
async def json_10k() -> Any:
    return parity_data.PAYLOADS["json-10k"]


@app.get("/db")
async def db_read(db: Db) -> list[UserOut]:
    return [UserOut.model_validate(u) for u in await parity_sqla.benchmark_users(db)]


@app.get("/articles")
async def list_articles(db: Db, page: int = 1, page_size: int = 20) -> ArticlePage:
    return await page_of(db, page, page_size)


@app.get("/articles/{article_id}")
async def get_article(article_id: int, db: Db) -> ArticleDetailOut:
    article = await parity_sqla.article_detail(db, article_id)
    if article is None:
        raise HTTPException(404, "Article not found")
    return ArticleDetailOut.model_validate(article)


@app.get("/auth/me")
async def auth_me(
    user: Annotated[parity_sqla.AuthUser, Depends(signed_in)],
) -> UserOut:
    return UserOut.model_validate(user)


@app.get("/auth/articles", dependencies=[Depends(signed_in)])
async def auth_articles(db: Db, page: int = 1, page_size: int = 20) -> ArticlePage:
    return await page_of(db, page, page_size)
