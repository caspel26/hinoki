"""Valid declarations: must type-check cleanly under mypy --strict."""

from uuid import UUID

from hinoki import API, Access, Hinoki, Permission, db, http
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


async def use(posts: Posts, request: Request) -> None:
    # Decorators preserve the exact signature and return type.
    text: str = await posts.retrieve(request, UUID(int=1))
    size: int = await posts.create(request, "x")
    label: str = health(request)
    # db.atomic keeps the parameters and the awaited result type.
    renamed: int = await rename(UUID(int=1), "x")
    await archive(UUID(int=1))
    async with db.atomic():
        pass
    print(text, size, label, renamed)


def factory() -> Hinoki:
    app = Hinoki()
    app.include(health, Posts)
    return app
