"""Invalid usage: each marked line must be rejected by the type checker.

``warn_unused_ignores`` turns a missing error into a failure, so this file
passes only while every line below is still flagged.
"""

from uuid import UUID

from hinoki import API, http
from hinoki.request import Request


class Posts(API, path="/posts"):
    @http.get("/{post_id}")
    async def retrieve(self, request: Request, post_id: UUID) -> str:
        return str(post_id)


async def misuse(posts: Posts, request: Request) -> None:
    await posts.retrieve(request, "not-a-uuid")  # type: ignore[arg-type]
    await posts.retrieve(request)  # type: ignore[call-arg]
    number: int = await posts.retrieve(request, UUID(int=1))  # type: ignore[assignment]
    print(number)


@http.get("/x", transaction="yes")  # type: ignore[arg-type]
def bad_option(request: Request) -> None: ...


@http.post("/x", access="admin")  # type: ignore[arg-type]
def bad_access(request: Request) -> None: ...
