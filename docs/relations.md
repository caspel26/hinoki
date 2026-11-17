---
status: implemented
---

# Relations, many-to-many and loading

Hinoki stores relations as ordinary foreign-key columns and loads related rows only when a query asks for them. Attribute access never performs hidden I/O.

## Declare a foreign key

```python
from uuid import UUID, uuid4

from hinoki.orm import Model, OnDelete, field, foreign_key


class Author(Model):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str


class Post(Model):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    author_id: UUID = foreign_key(
        Author,
        on_delete=OnDelete.RESTRICT,
        related_name="posts",
    )
    title: str
```

The `author_id` field is stored on `posts`. The forward relation is `post.author`; `author.posts` is the reverse relation. The foreign-key type must match the referenced primary-key type. `on_delete` is required, so what happens to a row when the row it references is deleted is always a decision. Its value is a member of the `OnDelete` enum (`from hinoki.orm import OnDelete`):

| `on_delete` | When the referenced row is deleted |
| --- | --- |
| `OnDelete.RESTRICT` | The delete is refused while referencing rows exist |
| `OnDelete.CASCADE` | The referencing rows are deleted too |
| `OnDelete.SET_NULL` | The column is set to `NULL`; the field must be annotated `| None` |
| `OnDelete.NO_ACTION` | Like `RESTRICT`, checked by the database at the end of the statement |

Only enum members are accepted. Text such as `on_delete="cascade"`, which earlier revisions took, is reported by a type checker and refused with a `RegistrationError` when the module is imported. `OnDelete.SET_NULL` on a field without `| None` is refused the same way, before any migration is planned:

```python
class Note(Model):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    post_id: UUID | None = foreign_key(Post, on_delete=OnDelete.SET_NULL, default=None)
```

The rule is written into the `REFERENCES ... ON DELETE` clause of the generated migration and enforced by the database.

## Foreign keys in abstract models and mixins

A foreign key declared in an [abstract model or a mixin](models.md#share-fields-abstract-models-and-mixins) is registered once for each table model that inherits it. The forward relation keeps its name on every model. The reverse name must differ, so write it with the `{model}` placeholder, which becomes the inheriting class's name in lower case:

```python
class Owned(Model, abstract=True):
    team_id: UUID = foreign_key(Team, on_delete=OnDelete.CASCADE, related_name="{model}_set")


class Article(Owned):
    id: UUID = field(primary_key=True, default_factory=uuid4)


class Page(Owned):
    id: UUID = field(primary_key=True, default_factory=uuid4)
```

`Team` gets the reverse relations `article_set` and `page_set`. With a fixed `related_name`, the second model that inherits the field is refused with an error naming both models. A table model that subclasses another table model copies the column and the forward relation; it gets a reverse relation of its own only when the name uses the placeholder, because a fixed name already belongs to its parent. A foreign key cannot reference an abstract model, which has no table. `{model}` is the only placeholder. Without `related_name` there is no reverse relation and nothing can collide.

## Load related rows explicitly

Use `select_related` for a forward foreign key when a query needs one related row (one `LEFT JOIN` in the same statement; `acount()` and `apaginate()` leave that join out of their `COUNT`, since loading a to-one relation cannot change the row count, but keep a join that a filter such as `author__name=...` needs). Use `prefetch_related` for a reverse or collection relation; it issues a separate bounded query and attaches the results.

```python
posts = await Post.all().select_related("author")
for post in posts:
    print(post.author.name)
```

Accessing an unloaded relation raises a relation-loading error. Load the relation explicitly so the query is visible in the calling code.

## One-to-one relations

A one-to-one relation uses the same foreign-key declaration with `unique=True`;
there is no separate field helper. The unique constraint enforces that each
target is referenced by at most one row. Keep the reverse relation explicit
with `related_name`:

```python
from __future__ import annotations

from typing import ClassVar
from uuid import UUID, uuid4

from hinoki.orm import Model, OnDelete, field, foreign_key
from hinoki.orm.relations import Related


class User(Model):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    profile: ClassVar[Related[Profile | None]]


class Profile(Model):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    user_id: UUID = foreign_key(
        User,
        on_delete=OnDelete.CASCADE,
        unique=True,
        related_name="profile",
    )
    user: ClassVar[Related[User]]
```

The forward `profile.user` relation has one required target. The reverse
`user.profile` relation is `Profile | None`: a user may not have a profile.
Load it explicitly and in a bounded number of queries:

```python
profiles = await Profile.all().select_related("user")
users = await User.all().prefetch_related("profile")
```

`select_related` joins the forward target into the profile query;
`prefetch_related` uses one batched query for profiles and returns one profile
or `None` per user. Neither access pattern performs lazy queries. If a database
has drifted and contains multiple rows despite the unique declaration,
`prefetch_related` raises `MultipleObjectsReturned` rather than choosing one.
Migrations create the FK and unique constraint together; applying a unique
constraint to existing duplicate data fails and requires an explicit data
cleanup before retrying. When changing an existing foreign key to `unique=True`,
SQLite rebuilds the table and may reject duplicates; MySQL, MariaDB and
PostgreSQL require a hand-written migration for the uniqueness change. Resolve
duplicates before applying the constraint.

Many-to-many relationships are represented by an explicit join model with two foreign keys and a composite `@unique` constraint. There is no implicit many-to-many table or `through=` shortcut.

## Model a many-to-many relationship

Define a model for the association table. Each row links one post to one tag; the composite uniqueness constraint prevents the same pair from being inserted twice.

```python
from typing import ClassVar
from uuid import UUID, uuid4

from hinoki.orm import Model, OnDelete, field, foreign_key, unique
from hinoki.orm.relations import Related


class Post(Model):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str
    tag_links: ClassVar["Related[list[PostTag]]"]


class Tag(Model):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str
    post_links: ClassVar["Related[list[PostTag]]"]


@unique("post_id", "tag_id")
class PostTag(Model):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    post_id: UUID = foreign_key(Post, on_delete=OnDelete.CASCADE, related_name="tag_links")
    tag_id: UUID = foreign_key(Tag, on_delete=OnDelete.CASCADE, related_name="post_links")
```

Create a link by inserting a `PostTag` row. To read a post's tags, query the join model and load its `tag` foreign key in the same query:

```python
post = await Post.acreate(title="Release notes")
tag = await Tag.acreate(name="docs")
await PostTag.acreate(post_id=post.id, tag_id=tag.id)

links = await PostTag.filter(post_id=post.id).select_related("tag")
tag_names = [link.tag.name for link in links]
```

The reverse `post.tag_links` relation contains `PostTag` rows; load it with `prefetch_related("tag_links")` before accessing it. It does not return `Tag` objects or fetch data on attribute access. A join model can also carry its own fields, such as the time a tag was added. Include all three models in the migration plan so the association table and its uniqueness constraint are created together.

This explicit form supports association rows and their foreign-key relations. Hinoki does not currently generate the join model or expose a direct `post.tags` relation.

See [models and queries](models.md) and [migrations](migrations.md) for schema changes to foreign keys.
