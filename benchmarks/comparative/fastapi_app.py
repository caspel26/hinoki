"""The benchmark service on FastAPI with SQLAlchemy 2 (async) and asyncpg."""

import math
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

import payloads
from fastapi import Depends, FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    joinedload,
    mapped_column,
    relationship,
)


class Base(DeclarativeBase):
    pass


class Author(Base):
    __tablename__ = "authors"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    name: Mapped[str]


class Post(Base):
    __tablename__ = "posts"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("authors.id"))
    title: Mapped[str]
    body: Mapped[str]
    published: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    author: Mapped[Author] = relationship(lazy="raise")


class AuthorOut(BaseModel):
    id: uuid.UUID
    name: str


class PostOut(BaseModel):
    id: uuid.UUID
    title: str
    body: str
    published: bool
    created_at: datetime
    author: AuthorOut


class PostIn(BaseModel):
    author_id: uuid.UUID
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1)


class PageOut(BaseModel):
    items: list[PostOut]
    page: int
    page_size: int
    total: int
    pages: int


engine = create_async_engine(
    os.environ["BENCH_DATABASE_URL"].replace("postgres://", "postgresql+asyncpg://"),
    pool_size=int(os.environ.get("BENCH_POOL", "10")),
    max_overflow=0,
    isolation_level="AUTOCOMMIT",
)
sessions = async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(lifespan=lifespan, openapi_url=None, docs_url=None, redoc_url=None)


async def session():
    async with sessions() as s:
        yield s


Db = Annotated[AsyncSession, Depends(session)]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/json-1k")
async def json_1k() -> Response:
    return Response(payloads.BODY_1K, media_type="application/json")


@app.get("/json-10k")
async def json_10k() -> Response:
    return Response(payloads.BODY_10K, media_type="application/json")


@app.get("/posts")
async def listing(
    db: Db,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PageOut:
    total = await db.scalar(
        select(func.count()).select_from(Post).where(Post.published.is_(True))
    )
    rows = (
        await db.scalars(
            select(Post)
            .options(joinedload(Post.author))
            .where(Post.published.is_(True))
            .order_by(Post.created_at.desc(), Post.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    ).all()
    return PageOut(
        items=[PostOut.model_validate(r, from_attributes=True) for r in rows],
        page=page,
        page_size=page_size,
        total=total or 0,
        pages=math.ceil((total or 0) / page_size),
    )


@app.get("/posts/{post_id}")
async def one(post_id: uuid.UUID, db: Db) -> PostOut:
    row = await db.scalar(
        select(Post)
        .options(joinedload(Post.author))
        .where(Post.id == post_id, Post.published.is_(True))
    )
    if row is None:
        raise HTTPException(404, "not found")
    return PostOut.model_validate(row, from_attributes=True)


@app.post("/posts", status_code=201)
async def create(data: PostIn, db: Db) -> PostOut:
    post = Post(
        id=uuid.uuid4(),
        author_id=data.author_id,
        title=data.title,
        body=data.body,
        published=True,
        created_at=datetime.now(UTC),
    )
    db.add(post)
    await db.flush()
    author = await db.get(Author, post.author_id)
    post.author = author  # already loaded: no further query
    await db.commit()
    return PostOut.model_validate(post, from_attributes=True)
