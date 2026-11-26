"""Valid declarations: must type-check cleanly under mypy --strict."""

import functools
from collections.abc import Awaitable, Callable
from uuid import UUID

from hinoki import API, Access, Hinoki, Permission, RouteGroup, db, http
from hinoki.http.request import Request
from tests.providers import TokenAuth


class PostPermission(Permission):
    WRITE = "posts:write"


editor = Access(auth=TokenAuth(), permissions={PostPermission.WRITE})


@http.get("/health")
def health(request: Request) -> str:
    return "ok"


class Posts(API, path="/posts"):
    @http.get("/{post_id}")
    async def retrieve(self, request: Request, post_id: UUID) -> str:
        return str(post_id)

    @http.post("/", status=201, access=editor, atomic=True)
    async def create(self, request: Request, title: str) -> int:
        return len(title)


@db.atomic
async def rename(post_id: UUID, title: str) -> int:
    return len(title)


@db.atomic()
async def archive(post_id: UUID) -> None: ...


def sync_double[**P](fn: Callable[P, int]) -> Callable[P, int]:
    @functools.wraps(fn)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> int:
        return fn(*args, **kwargs) * 2

    return wrapped


def async_double[**P](
    fn: Callable[P, Awaitable[int]],
) -> Callable[P, Awaitable[int]]:
    @functools.wraps(fn)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> int:
        return (await fn(*args, **kwargs)) * 2

    return wrapped


grouped = RouteGroup("/grouped", access=editor, atomic=True)


@grouped.get(
    "/public",
    access=None,
    atomic=False,
    cors=None,
    include_in_schema=False,
    tags=("v1",),
)
@sync_double
def grouped_sync(count: int) -> int:
    return count + 1


@grouped.post("/items", status=201)
@async_double
async def grouped_async(count: int) -> int:
    return count + 2


async def use(posts: Posts, request: Request) -> None:
    # Decorators preserve the exact signature and return type.
    text: str = await posts.retrieve(request, UUID(int=1))
    size: int = await posts.create(request, "x")
    label: str = health(request)
    doubled_sync: int = grouped_sync(3)
    doubled_async: int = await grouped_async(4)
    # db.atomic keeps the parameters and the awaited result type.
    renamed: int = await rename(UUID(int=1), "x")
    await archive(UUID(int=1))
    async with db.atomic():
        pass
    print(text, size, label, doubled_sync, doubled_async, renamed)


def factory() -> Hinoki:
    app = Hinoki()
    app.include(health, Posts, grouped)
    return app
