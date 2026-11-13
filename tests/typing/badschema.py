"""Invalid schema usage: every marked line must be rejected by mypy."""

from hinoki import Schema


class PostCreate(Schema):
    title: str
    body: str


def misuse() -> None:
    PostCreate(title="x")  # type: ignore[call-arg]
    PostCreate(title=3, body="x")  # type: ignore[arg-type]
    PostCreate(title="x", body="y", extra=1)  # type: ignore[call-arg]
    post = PostCreate(title="x", body="y")
    post.title = "other"  # type: ignore[misc]
    print(post.missing)  # type: ignore[attr-defined]
