---
status: implemented
---

# Relations, many-to-many and loading

Hinoki stores relations as ordinary foreign-key columns and loads related rows only when a query asks for them. Attribute access never performs hidden I/O.

## Declare a foreign key

```python
class Author(Model, table="authors"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str


class Post(Model, table="posts"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    author_id: UUID = foreign_key(
        Author,
        on_delete="restrict",
        related_name="posts",
    )
    title: str
```

The `author_id` field is stored on `posts`. The forward relation is `post.author`; `author.posts` is the reverse relation. The foreign-key type must match the referenced primary-key type. Choose an `on_delete` rule explicitly: `restrict`, `cascade`, `set_null` or `no_action`. A nullable `set_null` field should use an optional annotation.

## Load related rows explicitly

Use `select_related` for a forward foreign key when a query needs one related row. Use `prefetch_related` for a reverse or collection relation; it issues a separate bounded query and attaches the results.

```python
posts = await Post.all().select_related("author").aall()
for post in posts:
    print(post.author.name)
```

Accessing an unloaded relation raises a relation-loading error. Load the relation explicitly so the query is visible in the calling code.

Many-to-many relationships are represented by an explicit join model with two foreign keys and a composite `@unique` constraint. There is no implicit many-to-many table or `through=` shortcut.

## Model a many-to-many relationship

Define a model for the association table. Each row links one post to one tag; the composite uniqueness constraint prevents the same pair from being inserted twice.

```python
from typing import ClassVar
from uuid import UUID, uuid4

from hinoki.orm import Model, field, foreign_key, unique
from hinoki.orm.relations import Related


class Post(Model, table="posts"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str
    tag_links: ClassVar["Related[list[PostTag]]"]


class Tag(Model, table="tags"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str
    post_links: ClassVar["Related[list[PostTag]]"]


@unique("post_id", "tag_id")
class PostTag(Model, table="post_tags"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    post_id: UUID = foreign_key(Post, on_delete="cascade", related_name="tag_links")
    tag_id: UUID = foreign_key(Tag, on_delete="cascade", related_name="post_links")
```

Create a link by inserting a `PostTag` row. To read a post's tags, query the join model and load its `tag` foreign key in the same query:

```python
post = await Post.acreate(title="Release notes")
tag = await Tag.acreate(name="docs")
await PostTag.acreate(post_id=post.id, tag_id=tag.id)

links = await PostTag.filter(post_id=post.id).select_related("tag").aall()
tag_names = [link.tag.name for link in links]
```

The reverse `post.tag_links` relation contains `PostTag` rows; load it with `prefetch_related("tag_links")` before accessing it. It does not return `Tag` objects or fetch data on attribute access. A join model can also carry its own fields, such as the time a tag was added. Include all three models in the migration plan so the association table and its uniqueness constraint are created together.

This explicit form supports association rows and their foreign-key relations. Hinoki does not currently generate the join model or expose a direct `post.tags` relation.

See [models and queries](models.md) and [migrations](migrations.md) for schema changes to foreign keys.
