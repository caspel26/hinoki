"""Valid declarations: must type-check cleanly under mypy --strict."""

from uuid import UUID

from hinoki import API, Access, HinokiAPI, get, post

editor = Access(auth=object(), scopes={"posts:write"})


@get("/health")
def health() -> str:
    return "ok"


class Posts(API, path="/posts"):
    @get("/{post_id}")
    async def retrieve(self, post_id: UUID) -> str:
        return str(post_id)

    @post("/", status=201, access=editor, transaction=True)
    async def create(self, title: str) -> int:
        return len(title)


async def use(posts: Posts) -> None:
    # Decorators preserve the exact signature and return type.
    text: str = await posts.retrieve(UUID(int=1))
    size: int = await posts.create("x")
    label: str = health()
    print(text, size, label)


def factory() -> HinokiAPI:
    app = HinokiAPI()
    app.include(health, Posts)
    return app
