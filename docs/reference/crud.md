# Generated CRUD API

The [CRUD guide](../crud.md) explains action generation, access policies,
ownership, schema requirements and omitting generated actions.

::: hinoki.resources.crud

::: hinoki.resources.CrudAccess
    options:
      show_if_no_docstring: true

::: hinoki.resources.CrudDeclaration
    options:
      show_if_no_docstring: true

::: hinoki.resources.Crud

## Filters

A `FilterSet` declares what a client may ask of a generated list and is passed
as `filters=`; see [filter, order and search the
list](../crud.md#filter-order-and-search-the-list). Declare each field as
`Eq[T]`, `In[T]` or `Range[T]` (type aliases of `hinoki.filters`) and add a
`Search(...)` and an `ordering` tuple. A `Range` field reads as a `Bounds`.

::: hinoki.filters.FilterSet
    options:
      show_if_no_docstring: true

::: hinoki.filters.Search
    options:
      show_if_no_docstring: true

::: hinoki.filters.Bounds
    options:
      show_if_no_docstring: true

## Row scoping

::: hinoki.resources.RowScope
    options:
      show_if_no_docstring: true

::: hinoki.resources.all_rows

::: hinoki.resources.own_rows

::: hinoki.resources.from_user
