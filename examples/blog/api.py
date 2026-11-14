"""Endpoints: plain functions and one API class, sync and async."""

from uuid import UUID

from examples.blog.models import Post, User
from examples.blog.schemas import HealthOut, PostCreate, PostOut
from examples.blog.security import editor
from hinoki import Caller, Conflict, Forbidden, get, post
from hinoki.crud import CrudAPI, Resource
from hinoki.orm import QuerySet
from hinoki.pagination import Page, PageNumberPagination, PageParams


class PostAlreadyPublished(Conflict):
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
        raise Forbidden("You cannot change this post.")
    return post


@get("/health")
def health() -> HealthOut:
    """Liveness: answers while the worker runs."""
    return HealthOut(status="ok")


def all_with_author() -> QuerySet[Post]:
    return with_author(Post.all())


POSTS = Resource(
    model=Post,
    read=PostOut,
    create=PostCreate,
    deletable=True,
    access=editor,
    owner="author_id",
    queryset=all_with_author,
    output=to_out,
)


class Posts(CrudAPI, path="/posts", resource=POSTS):
    """Generated create and delete (owner enforced), handwritten reads."""

    @get(
        "/",
        replaces="list",
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

    @get("/{item_id}", replaces="retrieve")
    async def one(self, item_id: UUID) -> PostOut:
        """A published post; unpublished posts are not found."""
        return to_out(await with_author(Post.filter(id=item_id, published=True)).aget())

    @post(
        "/{item_id}/publish",
        access=editor,
        transaction=True,
        errors=(Forbidden, PostAlreadyPublished),
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
