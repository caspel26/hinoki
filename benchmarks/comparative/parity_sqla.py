"""SQLAlchemy 2 (async, asyncpg) models and queries shared by the FastAPI and
Litestar services.

Relations are ``lazy="raise"``: every load is explicit, so each request issues
the statements ``parity_verify.py`` expects (the author joined, tags and
comments in one ``selectin`` query each).
"""

import math
import os
from datetime import datetime
from typing import Any, ClassVar

import jwt
import parity_data
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Table,
    func,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    joinedload,
    mapped_column,
    relationship,
    selectinload,
)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {datetime: DateTime(timezone=True)}


article_tags = Table(
    "articles_article_tags",
    Base.metadata,
    Column("id", BigInteger, primary_key=True),
    Column("article_id", ForeignKey("articles_article.id")),
    Column("tag_id", ForeignKey("articles_tag.id")),
)


class BenchmarkUser(Base):
    __tablename__ = "benchmark_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str]
    email: Mapped[str]
    first_name: Mapped[str]
    last_name: Mapped[str]
    is_active: Mapped[bool]


class AuthUser(Base):
    __tablename__ = "auth_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str]
    email: Mapped[str]
    first_name: Mapped[str]
    last_name: Mapped[str]
    is_active: Mapped[bool]


class Author(Base):
    __tablename__ = "articles_author"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    email: Mapped[str]
    bio: Mapped[str]
    created_at: Mapped[datetime]


class Tag(Base):
    __tablename__ = "articles_tag"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    slug: Mapped[str]


class Comment(Base):
    __tablename__ = "articles_comment"
    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles_article.id"))
    author_name: Mapped[str]
    content: Mapped[str]
    created_at: Mapped[datetime]


class Article(Base):
    __tablename__ = "articles_article"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]
    content: Mapped[str]
    published: Mapped[bool]
    author_id: Mapped[int] = mapped_column(ForeignKey("articles_author.id"))
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    author: Mapped[Author] = relationship(lazy="raise")
    tags: Mapped[list[Tag]] = relationship(secondary=article_tags, lazy="raise")
    comments: Mapped[list[Comment]] = relationship(
        lazy="raise", order_by=Comment.created_at.desc()
    )


engine = create_async_engine(
    os.environ["BENCH_DATABASE_URL"].replace("postgres://", "postgresql+asyncpg://"),
    pool_size=int(os.environ.get("BENCH_POOL", "8")),
    max_overflow=0,
    # As in Django Bolt's comparison: test each connection on checkout.
    pool_pre_ping=True,
)
sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def clamp(page: int, page_size: int) -> tuple[int, int]:
    """Out-of-range values are clamped, as Django Bolt's handlers do."""
    return max(1, page), max(1, min(100, page_size))


async def articles_page(
    db: AsyncSession, page: int, page_size: int
) -> tuple[list[Article], int, int, int, int]:
    page, page_size = clamp(page, page_size)
    total = await db.scalar(select(func.count()).select_from(Article)) or 0
    rows = (
        await db.scalars(
            select(Article)
            .options(joinedload(Article.author), selectinload(Article.tags))
            .order_by(Article.created_at.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    ).all()
    return list(rows), total, page, page_size, math.ceil(total / page_size)


async def article_detail(db: AsyncSession, article_id: int) -> Article | None:
    return await db.scalar(
        select(Article)
        .options(
            joinedload(Article.author),
            selectinload(Article.tags),
            selectinload(Article.comments),
        )
        .where(Article.id == article_id)
    )


async def benchmark_users(db: AsyncSession) -> list[BenchmarkUser]:
    return list((await db.scalars(select(BenchmarkUser).limit(10))).all())


def token_subject(token: str | None) -> int | None:
    """The user id of a valid access token (PyJWT, HS256, ``exp`` required)."""
    if not token:
        return None
    try:
        claims = jwt.decode(
            token,
            parity_data.SECRET,
            algorithms=[parity_data.ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
    except jwt.InvalidTokenError:
        return None
    subject = claims["sub"]
    return int(subject) if isinstance(subject, str) and subject.isdigit() else None


async def current_user(db: AsyncSession, token: str | None) -> AuthUser | None:
    subject = token_subject(token)
    return None if subject is None else await db.get(AuthUser, subject)
