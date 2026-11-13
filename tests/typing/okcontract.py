"""Valid declarations: must type-check cleanly under mypy --strict."""

from uuid import UUID

from hinoki import API, Access, HinokiAPI, get, post
from hinoki.request import Request
from tests.providers import TokenAuth

editor = Access(auth=TokenAuth(), scopes={"posts:write"})


@get("/health")
def health(request: Request) -> str:
    return "ok"


class Posts(API, path="/posts"):
    @get("/{post_id}")
    async def retrieve(self, request: Request, post_id: UUID) -> str:
        return str(post_id)

    @post("/", status=201, access=editor, transaction=True)
    async def create(self, request: Request, title: str) -> int:
        return len(title)


async def use(posts: Posts, request: Request) -> None:
    # Decorators preserve the exact signature and return type.
    text: str = await posts.retrieve(request, UUID(int=1))
    size: int = await posts.create(request, "x")
    label: str = health(request)
    print(text, size, label)


def factory() -> HinokiAPI:
    app = HinokiAPI()
    app.include(health, Posts)
    return app
