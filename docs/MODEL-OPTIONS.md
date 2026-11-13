# Model options

Options that describe a model beyond its columns are **decorators**, read like the action decorators. Only what is needed when the class is created stays a class keyword.

```python
@unique("owner_id", "url")
@indexed("owner_id", "created_at")
@ordered_by("-created_at", "-id")
class Bookmark(Model, table="bookmarks", label="Bookmark", plural="Bookmarks"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    owner_id: UUID = foreign_key(Member, on_delete="restrict", related_name="bookmarks")
    url: str = field(max_length=500)
    created_at: datetime = field(default_factory=now)
```

| Option | Form | Effect |
| --- | --- | --- |
| table | `table="bookmarks"` (class keyword, required) | the table name |
| labels | `label=`, `plural=` (class keywords) | the singular and plural name the admin shows |
| composite uniqueness | `@unique("a", "b")` | a `UNIQUE (a, b)` constraint; a single column uses `field(unique=True)` |
| index | `@indexed("a", "b")` | a `CREATE INDEX` planned by the migration tool; adding or removing one is a non-destructive migration |
| default order | `@ordered_by("-created_at", "id")` | every query without `order_by` (lists, pages) is ordered this way; `"-"` is descending; an explicit `order_by` replaces it, counts and bulk updates ignore it |

* Column names are checked when the decorator is applied; a typo fails at import with the model and the unknown names.
* Stacked decorators are stored in reading order (top to bottom). A subclass does not inherit its parent's options.
* Apply options before the model is used: the migration planner, the query layer and the admin read them from the class.
* Index names are `ix_<table>_<columns>`, shortened deterministically (with a hash) past PostgreSQL's 63 bytes.

Not yet: partial or expression indexes, per-column index options, soft delete and automatic timestamps (planned as further decorators).
