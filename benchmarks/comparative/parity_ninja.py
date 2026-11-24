"""The benchmark service on Django Ninja (Uvicorn, Django ORM, psycopg pool).

The handlers follow Django Bolt's comparison app for Ninja: async handlers that
run the sync ORM work on a bounded executor of as many threads as pool
connections (``sync_to_async(thread_sensitive=False)``, releasing the connection
after each call), Pydantic schemas, and django-ninja-jwt validating the
``access_token`` cookie through Ninja's ``APIKeyCookie``.
"""

import math
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

import django
from asgiref.sync import sync_to_async
from django.core.asgi import get_asgi_application
from django.db import close_old_connections
from django.http import Http404
from django.urls import path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "parity_ninja_settings")
django.setup()

import parity_data  # noqa: E402
from ninja import NinjaAPI, Schema  # noqa: E402
from ninja.renderers import JSONRenderer  # noqa: E402
from ninja.security import APIKeyCookie  # noqa: E402
from ninja_jwt.authentication import AsyncJWTBaseAuthentication  # noqa: E402
from parityapp.models import Article, BenchmarkUser  # noqa: E402

POOL = int(os.environ.get("BENCH_POOL", "8"))


class CompactRenderer(JSONRenderer):
    """Ninja's renderer with compact separators, so every stack sends the same bytes."""

    json_dumps_params: dict[str, Any] = {"separators": (",", ":")}  # noqa: RUF012


class CookieJWTAuth(AsyncJWTBaseAuthentication, APIKeyCookie):
    """django-ninja-jwt's token validation and user lookup on the cookie.

    The lookup runs on the bounded ORM executor and releases its connection.
    django-ninja-jwt's own async path runs it in the request's thread-sensitive
    thread, which keeps the connection until the request ends; with 100
    concurrent requests and 8 pooled connections every connection is then held
    by a request that waits for a second one, and the service stalls until the
    pool times out.
    """

    param_name = parity_data.COOKIE

    def __init__(self) -> None:
        super().__init__()
        self.csrf = False  # only GET routes are measured, as in the other stacks

    async def authenticate(self, request: Any, key: str | None) -> Any:
        if not key:
            return None
        token = self.get_validated_token(key)
        request.user = await run_orm(self.get_user, token)
        return request.user


api = NinjaAPI(renderer=CompactRenderer(), openapi_url=None, docs_url=None)
cookie_auth = CookieJWTAuth()
orm_executor = ThreadPoolExecutor(max_workers=POOL, thread_name_prefix="orm")


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


class ArticleDetailOut(ArticleOut):
    comments: list[CommentOut]


class ArticlePage(Schema):
    items: list[ArticleOut]
    total: int
    page: int
    page_size: int
    pages: int


def released[T](function: Callable[..., T], *args: Any) -> T:
    """Release the thread's pooled connection after the call, as Django would at
    the end of a request on the request thread."""
    try:
        return function(*args)
    finally:
        close_old_connections()


async def run_orm[T](function: Callable[..., T], *args: Any) -> T:
    return await sync_to_async(released, thread_sensitive=False, executor=orm_executor)(
        function, *args
    )


def author_out(author: Any) -> dict[str, Any]:
    return {
        "id": author.id,
        "name": author.name,
        "email": author.email,
        "bio": author.bio,
        "created_at": author.created_at,
    }


def article_out(article: Article) -> dict[str, Any]:
    return {
        "id": article.id,
        "title": article.title,
        "content": article.content,
        "published": article.published,
        "author": author_out(article.author),
        "tags": [
            {"id": t.id, "name": t.name, "slug": t.slug} for t in article.tags.all()
        ],
        "created_at": article.created_at,
        "updated_at": article.updated_at,
    }


def fetch_users() -> list[BenchmarkUser]:
    return list(BenchmarkUser.objects.all()[:10])


def fetch_page(page: int, page_size: int) -> dict[str, Any]:
    total = Article.objects.count()
    offset = (page - 1) * page_size
    found = (
        Article.objects.select_related("author")
        .prefetch_related("tags")
        .order_by("-created_at")[offset : offset + page_size]
    )
    return {
        "items": [article_out(article) for article in found],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": math.ceil(total / page_size),
    }


def fetch_detail(article_id: int) -> dict[str, Any]:
    try:
        article = (
            Article.objects.select_related("author")
            .prefetch_related("tags", "comments")
            .get(id=article_id)
        )
    except Article.DoesNotExist:
        raise Http404("Article not found") from None
    comments = [
        {
            "id": c.id,
            "article_id": c.article_id,
            "author_name": c.author_name,
            "content": c.content,
            "created_at": c.created_at,
        }
        for c in article.comments.all()
    ]
    return {**article_out(article), "comments": comments}


async def articles_page(page: int, page_size: int) -> dict[str, Any]:
    page, page_size = max(1, page), max(1, min(100, page_size))
    return await run_orm(fetch_page, page, page_size)


@api.get("/health")
async def health(request: Any) -> dict[str, str]:
    return {"status": "ok"}


@api.get("/json-1k")
async def json_1k(request: Any) -> list[dict[str, Any]]:
    return parity_data.PAYLOADS["json-1k"]


@api.get("/json-10k")
async def json_10k(request: Any) -> list[dict[str, Any]]:
    return parity_data.PAYLOADS["json-10k"]


@api.get("/db", response=list[UserOut])
async def db_read(request: Any) -> list[BenchmarkUser]:
    return await run_orm(fetch_users)


@api.get("/articles", response=ArticlePage)
async def list_articles(request: Any, page: int = 1, page_size: int = 20) -> Any:
    return await articles_page(page, page_size)


@api.get("/articles/{article_id}", response=ArticleDetailOut)
async def get_article(request: Any, article_id: int) -> Any:
    return await run_orm(fetch_detail, article_id)


@api.get("/auth/me", response=UserOut, auth=cookie_auth)
async def auth_me(request: Any) -> Any:
    return request.auth


@api.get("/auth/articles", response=ArticlePage, auth=cookie_auth)
async def auth_articles(request: Any, page: int = 1, page_size: int = 20) -> Any:
    return await articles_page(page, page_size)


urlpatterns = [path("", api.urls)]
application = get_asgi_application()
