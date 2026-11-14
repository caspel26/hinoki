---
status: implemented
---

# Model options

Options that describe a model beyond its columns are **decorators**, read like the action decorators. Hinoki has no nested `class Meta`. Only what is needed when the class is created stays a class keyword.

```python
from datetime import datetime
from uuid import UUID, uuid4

from hinoki.orm import Model, field, timestamps
from hinoki.utils import utc_now


@unique("owner_id", "url")
@indexed("owner_id", "created_at")
@ordered_by("-created_at", "-id")
@timestamps()
class Bookmark(Model, table="bookmarks", label="Bookmark", plural="Bookmarks"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    owner_id: UUID = foreign_key(Member, on_delete="restrict", related_name="bookmarks")
    url: str = field(max_length=500)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
```

| Option | Form | Effect |
| --- | --- | --- |
| table | `table="bookmarks"` (class keyword, required) | the table name |
| labels | `label=`, `plural=` (class keywords) | the singular and plural name the admin shows |
| composite uniqueness | `@unique("a", "b")` | a `UNIQUE (a, b)` constraint; a single column uses `field(unique=True)` |
| index | `@indexed("a", "b")` | a `CREATE INDEX` planned by the migration tool; adding or removing one is a non-destructive migration |
| default order | `@ordered_by("-created_at", "id")` | every query without `order_by` (lists, pages) is ordered this way; `"-"` is descending; an explicit `order_by` replaces it, counts and bulk updates ignore it |
| update timestamp | `@timestamps()` | refresh a declared `updated_at: datetime` field for every ORM update statement; use `@timestamps("modified_at", clock=...)` to customize |

* Column names are checked when the decorator is applied; a typo fails at import with the model and the unknown names.
* Stacked decorators are stored in reading order (top to bottom). A subclass does not inherit its parent's options.
* Apply options before the model is used: the migration planner, the query layer and the admin read them from the class.
* Index names are `ix_<table>_<columns>`, shortened deterministically (with a hash) past PostgreSQL's 63 bytes.

Not yet: partial or expression indexes, per-column index options, and soft delete. For timestamps, see [utilities](utilities.md); fields are declared explicitly and migrations remain visible.
