# Relations and loading

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

Without an explicit load, relation access raises a relation-loading error rather than querying the database. This makes query count visible in the calling code.

Many-to-many relationships are represented by an explicit join model with two foreign keys and a composite `@unique` constraint. There is no implicit many-to-many table or `through=` shortcut.

See [models and queries](MODELS.md) and [migrations](MIGRATIONS.md) for schema changes to foreign keys.
