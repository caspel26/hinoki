"""The benchmark service on Django Bolt (Actix server, Django ORM, psycopg pool).

The handlers follow Django Bolt's own comparison app: async handlers that run the
sync ORM work in Bolt's bounded thread pool (``sync_to_thread``), msgspec
structs, and Bolt's ``JWTAuthentication`` reading the ``access_token`` cookie.
"""

import math
from datetime import datetime
from typing import Any

import msgspec
import parity_data
from django_bolt import BoltAPI
from django_bolt.auth import IsAuthenticated, JWTAuthentication, get_current_user
from django_bolt.concurrency import sync_to_thread
from django_bolt.exceptions import NotFound

from parityapp.models import Article, BenchmarkUser

api = BoltAPI(enable_logging=False, compression=False)

cookie_auth = JWTAuthentication(
    secret=parity_data.SECRET,
    algorithms=[parity_data.ALGORITHM],
    cookie=parity_data.COOKIE,
)


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


def user_out(user: Any) -> UserOut:
    return UserOut(
        id=user.id,
        username=user.username,
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        is_active=user.is_active,
    )


def author_out(author: Any) -> AuthorOut:
    return AuthorOut(
        id=author.id,
        name=author.name,
        email=author.email,
        bio=author.bio,
        created_at=author.created_at,
    )


def tags_out(article: Article) -> list[TagOut]:
    return [TagOut(id=t.id, name=t.name, slug=t.slug) for t in article.tags.all()]


def fetch_page(page: int, page_size: int) -> ArticlePage:
    total = Article.objects.count()
    offset = (page - 1) * page_size
    found = list(
        Article.objects.select_related("author")
        .prefetch_related("tags")
        .order_by("-created_at")[offset : offset + page_size]
    )
    return ArticlePage(
        items=[
            ArticleOut(
                id=a.id,
                title=a.title,
                content=a.content,
                published=a.published,
                author=author_out(a.author),
                tags=tags_out(a),
                created_at=a.created_at,
                updated_at=a.updated_at,
            )
            for a in found
        ],
        total=total,
        page=page,
        page_size=page_size,
        pages=math.ceil(total / page_size),
    )


async def articles_page(page: int, page_size: int) -> ArticlePage:
    page, page_size = max(1, page), max(1, min(100, page_size))
    return await sync_to_thread(fetch_page, page, page_size)


def fetch_detail(article_id: int) -> ArticleDetailOut:
    try:
        a = (
            Article.objects.select_related("author")
            .prefetch_related("tags", "comments")
            .get(id=article_id)
        )
    except Article.DoesNotExist:
        raise NotFound(detail="Article not found") from None
    return ArticleDetailOut(
        id=a.id,
        title=a.title,
        content=a.content,
        published=a.published,
        author=author_out(a.author),
        tags=tags_out(a),
        comments=[
            CommentOut(
                id=c.id,
                article_id=c.article_id,
                author_name=c.author_name,
                content=c.content,
                created_at=c.created_at,
            )
            for c in a.comments.all()
        ],
        created_at=a.created_at,
        updated_at=a.updated_at,
    )


def fetch_users() -> list[UserOut]:
    return [user_out(u) for u in BenchmarkUser.objects.all()[:10]]


@api.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@api.get("/json-1k")
async def json_1k() -> list[dict[str, Any]]:
    return parity_data.PAYLOADS["json-1k"]


@api.get("/json-10k")
async def json_10k() -> list[dict[str, Any]]:
    return parity_data.PAYLOADS["json-10k"]


@api.get("/db", response_model=list[UserOut])
async def db_read() -> list[UserOut]:
    return await sync_to_thread(fetch_users)


@api.get("/articles", response_model=ArticlePage)
async def list_articles(page: int = 1, page_size: int = 20) -> ArticlePage:
    return await articles_page(page, page_size)


@api.get("/articles/{article_id}", response_model=ArticleDetailOut)
async def get_article(article_id: int) -> ArticleDetailOut:
    return await sync_to_thread(fetch_detail, article_id)


@api.get(
    "/auth/me",
    auth=[cookie_auth],
    guards=[IsAuthenticated()],
    response_model=UserOut,
)
async def auth_me(request) -> UserOut:
    return user_out(await get_current_user(request))


@api.get(
    "/auth/articles",
    auth=[cookie_auth],
    guards=[IsAuthenticated()],
    response_model=ArticlePage,
)
async def auth_articles(request, page: int = 1, page_size: int = 20) -> ArticlePage:
    # Load the user like the other stacks' auth layers do, although unused here.
    await get_current_user(request)
    return await articles_page(page, page_size)
