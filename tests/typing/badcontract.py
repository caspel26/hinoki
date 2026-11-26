"""Invalid usage: each marked line must be rejected by the type checker.

``warn_unused_ignores`` turns a missing error into a failure, so this file
passes only while every line below is still flagged.
"""

from uuid import UUID

from hinoki import API, RouteGroup, db, http
from hinoki.http.request import Request


class Posts(API, path="/posts"):
    @http.get("/{post_id}")
    async def retrieve(self, request: Request, post_id: UUID) -> str:
        return str(post_id)


async def misuse(posts: Posts, request: Request) -> None:
    await posts.retrieve(request, "not-a-uuid")  # type: ignore[arg-type]
    await posts.retrieve(request)  # type: ignore[call-arg]
    number: int = await posts.retrieve(request, UUID(int=1))  # type: ignore[assignment]
    print(number)


@http.get("/x", atomic="yes")  # type: ignore[arg-type]
def bad_option(request: Request) -> None: ...


@http.post("/x", access="admin")  # type: ignore[arg-type]
def bad_access(request: Request) -> None: ...


group = RouteGroup("/group")


@group.post("/x", atomic="yes")  # type: ignore[arg-type]
def bad_group_option(request: Request) -> None: ...


@group.post("/y", stauts=201)  # type: ignore[call-arg]
def bad_group_keyword(request: Request) -> None: ...


@group.get("/z", access="admin")  # type: ignore[arg-type]
def bad_group_access(request: Request) -> None: ...


@group.get("/schema", include_in_schema=None)  # type: ignore[arg-type]
def bad_group_schema(request: Request) -> None: ...


@db.atomic
async def renamed(post_id: UUID) -> str:
    return str(post_id)


async def misuse_atomic() -> None:
    await renamed("not-a-uuid")  # type: ignore[arg-type]
    count: int = await renamed(UUID(int=1))  # type: ignore[assignment]
    print(count)


@db.atomic
def sync_atomic(name: str) -> int:
    return len(name)


def use_sync_atomic() -> None:
    size: int = sync_atomic("x")
    wrong: str = sync_atomic("x")  # type: ignore[assignment]
    print(size, wrong)
