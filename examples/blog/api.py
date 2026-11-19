"""Route functions and one API class, sync and async."""

from uuid import UUID

from hinoki import Caller, crud, errors, http
from hinoki.orm import QuerySet
from hinoki.pagination import Page, PageNumberPagination, PageParams

from .models import Post, User
from .schemas import HealthOut, PostCreate, PostOut
from .security import editor


class PostAlreadyPublished(errors.Conflict):
    code = "post_already_published"
    message = "The post is already published."


def to_out(post: Post) -> PostOut:
    """The response schema for a row whose author was loaded explicitly."""
    return PostOut.from_model(post, author_name=post.author.name)


def with_author(query: QuerySet[Post]) -> QuerySet[Post]:
    """Relations are never lazy: every response that shows the author loads it."""
    return query.select_related("author")


async def owned_post(item_id: UUID, principal: Caller[User]) -> Post:
    """The post, if the caller wrote it. Scopes alone never grant row access."""
    post = await Post.aget(id=item_id)
    if post.author_id != principal.user.id:
        raise errors.Forbidden("You cannot change this post.")
    return post


@http.get("/health")
def health() -> HealthOut:
    """Liveness: answers while the worker runs."""
    return HealthOut(status="ok")


@crud(
    Post,
    path="/posts",
    read=PostOut,
    create=PostCreate,
    delete=True,
    access=editor,
    owner="author_id",
    omit=("list", "retrieve"),
)
class Posts:
    """Generated create and delete (owner enforced), handwritten reads."""

    async def to_schema(self, post: Post) -> PostOut:
        return to_out(await with_author(Post.filter(id=post.id)).aget())

    @http.get(
        "/",
        pagination=PageNumberPagination(page_size=10, max_page_size=50),
    )
    async def listing(self, page: PageParams) -> Page[PostOut]:
        """Published posts, newest first."""
        found = (
            await with_author(Post.filter(published=True))
            .order_by("-created_at", "-id")
            .apaginate(page)
        )
        return found.map(to_out)

    @http.get("/{item_id}")
    async def one(self, item_id: UUID) -> PostOut:
        """A published post; unpublished posts are not found."""
        return to_out(await with_author(Post.filter(id=item_id, published=True)).aget())

    @http.post(
        "/{item_id}/publish",
        access=editor,
        atomic=True,
        errors=(errors.Forbidden, PostAlreadyPublished),
    )
    async def publish(self, item_id: UUID, principal: Caller[User]) -> PostOut:
        """Publish an own draft. Publishing twice is a conflict."""
        changed = await Post.aupdate_where(
            {
                "id": item_id,
                "author_id": principal.user.id,
                "published": False,
            },
            {"published": True},
        )
        if not changed:
            # Keep the API's distinct 403 for another author and 404 for a
            # missing row. If it is still ours, the draft was already published.
            await owned_post(item_id, principal)
            raise PostAlreadyPublished()
        return to_out(await with_author(Post.filter(id=item_id)).aget())
