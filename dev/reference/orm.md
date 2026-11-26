# Models and queries

Concrete models may omit `@db.table(...)` (or the compatible `table=` class
keyword) to derive the table name `<app>_<models>` from the model's package and class name;
`label=` and `plural=` override humanized display names. Explicit table names
remain the safe choice for migration-backed histories and stable physical names.

- `hinoki.orm.Model()`: Base class: ``class Post(Model): ...`` with an optional ``table=`` override. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.Model)

- `hinoki.orm.field(...)`: Declare a column and, optionally, write-time constraints and validators. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.field)

- `hinoki.orm.pk(...)`: Declare the primary key: shorthand for ``field(primary_key=True, ...)``. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.pk)

Model write validation raises `ModelValidationError` from `hinoki.errors`, with field paths, stable codes and messages in `details`. See [Model field constraints and validators](https://hinoki.caspel.dev/dev/models.md#model-field-constraints-and-validators).

- `hinoki.orm.foreign_key(...)`: Declare a column that references ``model``'s primary key. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.foreign_key)

- `hinoki.orm.OnDelete(*values)`: What the database does with the rows that reference a deleted row. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.OnDelete)

- `hinoki.orm.unique(*columns: 'str', name: 'str | None' = None) -> 'Callable[[M], M]'`: A composite uniqueness constraint over two or more columns (single columns use ``field(unique=True)``). [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.unique)

- `hinoki.orm.indexed(*columns: 'str') -> 'Callable[[M], M]'`: A (non-unique) index over one or more columns, created by the migration planner. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.indexed)

- `hinoki.orm.table(name: 'str') -> 'Callable[[M], M]'`: Name the model's table; without it the name is derived from the class name. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.table)

- `hinoki.orm.ordered_by(*names: 'str') -> 'Callable[[M], M]'`: The default order of every query that does not call ``order_by``; ``"-created_at"`` is descending. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.ordered_by)

- `hinoki.orm.timestamps(updated_at: 'str' = 'updated_at', *, clock: 'Clock' = utc_now) -> 'Callable[[M], M]'`: Update a declared datetime column on each ORM update statement. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.timestamps)

- `hinoki.orm.QuerySet(...)`: Lazy, immutable query description; nothing runs until an ``a...`` call. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.QuerySet)

- `hinoki.pagination.PageParams(page: 'int', page_size: 'int') -> None`: Validated request for one page (1-based). [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.pagination.PageParams)

- `hinoki.pagination.Page(items: 'list[T]', page: 'int', page_size: 'int', total: 'int') -> None`: One page of results plus the totals needed to navigate. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.pagination.Page)

- `hinoki.pagination.PageNumberPagination(page_size: 'int | None' = None, max_page_size: 'int | None' = None) -> None`: Strategy: ``?page=N&page_size=M`` with a hard upper bound on the size. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.pagination.PageNumberPagination)

- `hinoki.pagination.CursorParams(limit: 'int', token: 'str | None', strategy: 'CursorPagination') -> None`: Validated request for one keyset page: the size and the raw token. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.pagination.CursorParams)

- `hinoki.pagination.CursorPage(items: 'list[T]', next: 'str | None', previous: 'str | None', page_size: 'int') -> None`: One keyset page: ``next``/``previous`` are cursors, or ``None`` at an end. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.pagination.CursorPage)

- `hinoki.pagination.CursorPagination(page_size: 'int | None' = None, max_page_size: 'int | None' = None, secret: 'str | None' = None, ttl: 'int | None' = None) -> None`: Strategy: ``?cursor=TOKEN&limit=N``, keyset pages with signed cursors. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.pagination.CursorPagination)

- `hinoki.orm.Related(name: 'str', relation: 'Relation') -> 'None'`: Class attribute that serves a loaded relation from the instance. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.Related)

## Mixins, options and helpers

- `hinoki.orm.TimestampedModel(*, created_at: 'datetime' = <factory>, updated_at: 'datetime' = <factory>) -> None`: Abstract base with ordinary ``created_at`` and ``updated_at`` columns. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.TimestampedModel)

- `hinoki.orm.SoftDeleteModel(*, deleted_at: 'datetime | None' = None) -> None`: Abstract base with an indexed nullable ``deleted_at`` column. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.SoftDeleteModel)

- `hinoki.orm.UUIDModel(*, id: 'UUID' = <factory>) -> None`: Abstract base with a UUIDv4 primary key column ``id``. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.UUIDModel)

- `hinoki.orm.soft_delete(column: 'str' = 'deleted_at', *, clock: 'Clock | None' = None) -> 'Callable[[M], M]'`: Mark a nullable datetime column as the model's soft-delete marker. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.soft_delete)

- `hinoki.orm.track_status(field: 'str' = 'status', *, changed_at: 'str' = 'status_changed_at', clock: 'Clock | None' = None) -> 'Callable[[M], M]'`: Record when a declared status column changes; decorators may be stacked. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.track_status)

- `hinoki.orm.many_to_many(...)`: Declare a many-to-many relation on a model::. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.many_to_many)

- `hinoki.orm.uuid_pk(...)`: Declare a UUID primary key. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.uuid_pk)

- `hinoki.orm.urlsafe_token(...)`: Declare a URL-safe random token column backed by Rust's CSPRNG (``hinoki.core.make_token``). [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.urlsafe_token)

- `hinoki.orm.uuid7() -> 'UUID'`: Return a time-ordered UUID version 7 (RFC 9562), generated in Rust. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.uuid7)

- `hinoki.orm.current_database() -> 'Database'`. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.current_database)

- `hinoki.orm.use_database(database: 'Database') -> 'Iterator[Database]'`: Make ``database`` the default for models inside the block (the ASGI adapter does this for every request when a ``Database`` is provided). [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.use_database)

- `hinoki.orm.Loading(...)`: Relations a query joins for filtering, joins and loads, or prefetches. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.Loading)

- `hinoki.orm.Relation(name: 'str', model: 'type[Model]', column: 'str', forward: 'bool', one_to_one: 'bool' = False) -> None`: One side of a foreign key. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.Relation)

- `hinoki.orm.ManyToMany(declaration: 'Declaration | None' = None) -> 'None'`: The declaration (``many_to_many(Tag)``) and, once bound, the attribute that gives each instance its ``LinkSet``. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.ManyToMany)

- `hinoki.orm.LinkKey`: Represent a PEP 604 union type. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.LinkKey)

- `hinoki.orm.LinkSet(owner: 'Model', side: 'LinkSide') -> 'None'`: ``post.tags``: the links of one owner. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.LinkSet)

- `hinoki.orm.LinkChange(added: 'tuple[LinkKey, ...]' = (), removed: 'tuple[LinkKey, ...]' = ()) -> None`: The target keys a write linked and unlinked, in ascending order. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.LinkChange)

- `hinoki.orm.Change(old: 'Any', new: 'Any') -> None`: One column's stored and current value; ``old`` is ``UNSET`` for a row that has not been written yet. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.Change)

- `hinoki.orm.StatusTrack(field: 'str', changed_at: 'str', clock: 'Clock | None' = None) -> None`: A status column and the datetime column recording its last change. [reference](https://hinoki.caspel.dev/dev/reference/orm/#hinoki.orm.StatusTrack)
