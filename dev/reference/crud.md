# Generated CRUD API

The [CRUD guide](https://hinoki.caspel.dev/dev/crud.md) explains action generation, access policies,
ownership, schema requirements and route replacement.

- `hinoki.resources.crud(...)`: Declare generated CRUD for ``model``. [reference](https://hinoki.caspel.dev/dev/reference/crud/#hinoki.resources.crud)

- `hinoki.resources.CrudAccess(...)`: Per-action access for ``crud(access=...)``. [reference](https://hinoki.caspel.dev/dev/reference/crud/#hinoki.resources.CrudAccess)

- `hinoki.resources.CrudDeclaration(config: '_CrudConfig', path: 'str', module: 'str' = 'hinoki.resources', name: 'str' = '') -> 'None'`: What ``crud(...)`` returns: include it for plain CRUD, or apply it to a class that adds hooks and handwritten actions. [reference](https://hinoki.caspel.dev/dev/reference/crud/#hinoki.resources.CrudDeclaration)

- `hinoki.resources.Crud(*values)`: The generated actions ``replaces=`` can name (each is also its plain string). [reference](https://hinoki.caspel.dev/dev/reference/crud/#hinoki.resources.Crud)

## Filters

A `FilterSet` declares what a client may ask of a generated list and is passed
as `filters=`; see [filter, order and search the
list](https://hinoki.caspel.dev/dev/crud.md#filter-order-and-search-the-list). Declare each field as
`Eq[T]`, `In[T]` or `Range[T]` (type aliases of `hinoki.filters`) and add a
`Search(...)` and an `ordering` tuple. A `Range` field reads as a `Bounds`.

- `hinoki.filters.FilterSet(parsed: 'core.FilterParsed') -> 'None'`: Base class of a list's declared filters. [reference](https://hinoki.caspel.dev/dev/reference/crud/#hinoki.filters.FilterSet)

- `hinoki.filters.Search(*columns: 'str') -> 'None'`: ``?search=text``: a case-insensitive "contains" over these text columns, any of them matching. [reference](https://hinoki.caspel.dev/dev/reference/crud/#hinoki.filters.Search)

- `hinoki.filters.Bounds(from_: 'T | None' = None, to: 'T | None' = None) -> None`: Both ends, inclusive, of a ``Range`` filter; a missing end is ``None``. [reference](https://hinoki.caspel.dev/dev/reference/crud/#hinoki.filters.Bounds)

## Row scoping

- `hinoki.resources.RowScope(model: 'type[M]', column: 'str', _source: "Literal['user', 'claim', 'caller']", _key: 'UserAttr | Callable[[U], Any] | str | None' = None) -> None`: Declarative mandatory row boundary for CRUD, manual actions, and links. [reference](https://hinoki.caspel.dev/dev/reference/crud/#hinoki.resources.RowScope)

- `hinoki.resources.all_rows(model: 'type[T]') -> 'Callable[[object], QuerySet[T]]'`: A ``links=`` entry that lets the caller link every row of ``model``: the explicit choice for targets that are not private to anyone. [reference](https://hinoki.caspel.dev/dev/reference/crud/#hinoki.resources.all_rows)

- `hinoki.resources.own_rows(model: 'type[T]', column: 'str') -> 'Callable[[object], QuerySet[T]]'`: A ``links=`` entry that lets the caller link and see only the rows of ``model`` whose text ``column`` holds the caller's id (``Caller.id``). [reference](https://hinoki.caspel.dev/dev/reference/crud/#hinoki.resources.own_rows)

- `hinoki.resources.from_user(attr: 'str') -> 'UserAttr'`: Extract ``attr`` (or a dotted attribute path) from ``Caller.user`` for ``crud(..., assign={...})`` or ``identity_key=...``. [reference](https://hinoki.caspel.dev/dev/reference/crud/#hinoki.resources.from_user)
