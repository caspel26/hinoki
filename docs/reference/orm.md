---
description: API reference for `Model`, columns, relations, indexes and querysets.
---

# Models and queries

Concrete models may omit `@db.table(...)` (or the compatible `table=` class
keyword) to derive the table name `<app>_<models>` from the model's package and class name;
`label=` and `plural=` override humanized display names. Explicit table names
remain the safe choice for migration-backed histories and stable physical names.

::: hinoki.orm.Model
    options:
      show_if_no_docstring: true
      inherited_members: false

::: hinoki.orm.field

::: hinoki.orm.pk

Model write validation raises `ModelValidationError` from `hinoki.errors`, with field paths, stable codes and messages in `details`. See [Model field constraints and validators](../models.md#model-field-constraints-and-validators).

::: hinoki.orm.foreign_key

::: hinoki.orm.OnDelete

::: hinoki.orm.unique

::: hinoki.orm.indexed

::: hinoki.orm.table

::: hinoki.orm.ordered_by

::: hinoki.orm.timestamps

::: hinoki.orm.QuerySet
    options:
      show_if_no_docstring: true

::: hinoki.pagination.PageParams

::: hinoki.pagination.Page
    options:
      show_if_no_docstring: true

::: hinoki.pagination.PageNumberPagination

::: hinoki.pagination.CursorParams

::: hinoki.pagination.CursorPage
    options:
      show_if_no_docstring: true

::: hinoki.pagination.CursorPagination

::: hinoki.orm.Related

## Mixins, options and helpers

::: hinoki.orm.TimestampedModel

::: hinoki.orm.SoftDeleteModel

::: hinoki.orm.UUIDModel

::: hinoki.orm.soft_delete

::: hinoki.orm.track_status

::: hinoki.orm.many_to_many

::: hinoki.orm.uuid_pk

::: hinoki.orm.urlsafe_token

::: hinoki.orm.uuid7

::: hinoki.orm.current_database

::: hinoki.orm.use_database

::: hinoki.orm.Loading

::: hinoki.orm.Relation

::: hinoki.orm.ManyToMany

::: hinoki.orm.LinkKey

::: hinoki.orm.LinkSet

::: hinoki.orm.LinkChange

::: hinoki.orm.Change

::: hinoki.orm.StatusTrack
