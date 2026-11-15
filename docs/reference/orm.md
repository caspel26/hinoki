# Models and queries

::: hinoki.orm.Model
    options:
      members: true
      show_if_no_docstring: true
      inherited_members: false

::: hinoki.orm.field

Model write validation raises `ModelValidationError` from `hinoki.errors`, with field paths, stable codes and messages in `details`. See [Model field constraints and validators](../models.md#model-field-constraints-and-validators).

::: hinoki.orm.foreign_key

::: hinoki.orm.OnDelete

::: hinoki.orm.unique

::: hinoki.orm.indexed

::: hinoki.orm.ordered_by

::: hinoki.orm.timestamps

::: hinoki.orm.QuerySet
    options:
      members: true
      show_if_no_docstring: true

::: hinoki.pagination.PageParams

::: hinoki.pagination.Page
    options:
      members: true
      show_if_no_docstring: true

::: hinoki.pagination.PageNumberPagination

::: hinoki.orm.Related
