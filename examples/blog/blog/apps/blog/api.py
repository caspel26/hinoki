"""Route functions and one API class: generated writes, handwritten reads.

The tenant is the caller's publication: every action that needs a signed-in
editor stays inside it (``get_queryset``, ``assign``, ``drafts``, ``export``).
"""

import csv
import io
from collections.abc import Iterator
from uuid import UUID

from hinoki import API, Caller, crud, errors, events, from_user, http
from hinoki.http import Stream
from hinoki.orm import QuerySet
from hinoki.pagination import Page, PageNumberPagination, PageParams

from .events import PostPublished
from .models import Post, Tag, User
from .schemas import HealthOut, PostCreate, PostOut, TagCreate, TagOut
from .security import editor

EXPORT_LIMIT = 1000  # a bounded export: the newest posts of the publication


class PostAlreadyPublished(errors.Conflict):
    code = "post_already_published"
    message = "The post is already published."


def to_out(post: Post) -> PostOut:
    """The response schema for a row whose author was loaded explicitly."""
    return PostOut.from_model(post, author_name=post.author.name)


def with_author(query: QuerySet[Post]) -> QuerySet[Post]:
    """Relations are never lazy: a response loads the author and the tag ids."""
    return query.select_related("author").prefetch_related("tags")


def tags_of(user: User | None) -> QuerySet[Tag]:
    """The tags a user may link and see: their publication's, none if anonymous."""
    if user is None:
        return Tag.filter(id__in=[])
    return Tag.filter(publication_id=user.publication_id)


def in_publication(principal: Caller[User]) -> QuerySet[Post]:
    """Every post of the caller's publication, the tenant boundary."""
    return Post.filter(publication_id=principal.user.publication_id)


async def owned_post(item_id: UUID, principal: Caller[User]) -> Post:
    """The post, if the caller wrote it. Scopes alone never grant row access.

    A post of another publication is a 404, as if it did not exist.
    """
    post = await in_publication(principal).filter(id=item_id).aget()
    if post.author_id != principal.user.id:
        raise errors.Forbidden("You cannot change this post.")
    return post


def csv_rows(posts: list[Post]) -> Iterator[str]:
    yield "slug,title,published\r\n"
    for post in posts:
        buffer = io.StringIO()
        csv.writer(buffer).writerow([post.slug, post.title, post.published])
        yield buffer.getvalue()


@http.get("/health")
async def health() -> HealthOut:
    """Liveness: answers while the worker runs."""
    return HealthOut(status="ok")


@crud(
    Tag,
    path="/tags",
    read=TagOut,
    create=TagCreate,
    access=editor,
    assign={"publication_id": from_user("publication_id")},
)
class Tags(API):
    """Tags of the caller's publication: listed and created by its editors."""

    def get_queryset(self, principal: Caller[User] | None) -> QuerySet[Tag]:
        return tags_of(None if principal is None else principal.user)


@crud(
    Post,
    path="/posts",
    read=PostOut,
    create=PostCreate,
    delete=True,
    access=editor,
    owner="author_id",
    assign={"publication_id": from_user("publication_id")},
    omit=("list", "retrieve"),
    links={"tags": tags_of},
)
class Posts(API):
    """Generated create and delete (owner and tenant enforced), handwritten reads."""

    def get_queryset(self, principal: Caller[User] | None) -> QuerySet[Post]:
        if principal is None:
            return Post.all()
        return in_publication(principal)

    async def to_schema(self, post: Post) -> PostOut:
        return to_out(await with_author(Post.filter(id=post.id)).aget())

    @http.get(
        "/",
        access=None,
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

    @http.get(
        "/drafts",
        access=editor,
        pagination=PageNumberPagination(page_size=10, max_page_size=50),
    )
    async def drafts(self, page: PageParams, principal: Caller[User]) -> Page[PostOut]:
        """The unpublished posts of the caller's publication."""
        found = (
            await with_author(in_publication(principal).filter(published=False))
            .order_by("-created_at", "-id")
            .apaginate(page)
        )
        return found.map(to_out)

    @http.get("/export.csv", access=editor)
    async def export(self, principal: Caller[User]) -> Stream:
        """The newest posts of the caller's publication as a CSV download."""
        posts = await (
            in_publication(principal).order_by("-created_at", "-id").limit(EXPORT_LIMIT)
        )
        return Stream(
            csv_rows(posts),
            content_type="text/csv; charset=utf-8",
            headers={"content-disposition": 'attachment; filename="posts.csv"'},
        )

    @http.get("/{item_id}", access=None)
    async def one(self, item_id: UUID) -> PostOut:
        """A published post; unpublished posts are not found."""
        return to_out(await with_author(Post.filter(id=item_id, published=True)).aget())

    @http.post(
        "/{item_id}/publish",
        access=editor,
        atomic=True,
        errors=(errors.NotFound, errors.Forbidden, PostAlreadyPublished),
    )
    async def publish(self, item_id: UUID, principal: Caller[User]) -> PostOut:
        """Publish an own draft. Publishing twice is a conflict."""
        changed = (
            await in_publication(principal)
            .filter(id=item_id, author_id=principal.user.id, published=False)
            .aupdate({"published": True}, returning=False)
        )
        if not changed:
            # Keep the API's distinct 403 for another author and 404 for a
            # missing row or another publication. If it is still ours, the draft
            # was already published.
            await owned_post(item_id, principal)
            raise PostAlreadyPublished()
        await events.emit(PostPublished(post_id=item_id))
        return to_out(await with_author(Post.filter(id=item_id)).aget())
