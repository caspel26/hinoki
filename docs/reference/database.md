# Database API

The ORM supports PostgreSQL and SQLite. SQLite is the local-development
default; configure an explicit URL for PostgreSQL or deployments. Raw SQL uses
the selected driver's placeholders: PostgreSQL uses ``$1``, ``$2``, and SQLite
uses ``?1``, ``?2``. ORM-generated queries select the dialect automatically.

::: hinoki.db.Database
    options:
      members: true
      show_if_no_docstring: true

::: hinoki.db.DatabaseModule
    options:
      members: true
      show_if_no_docstring: true

::: hinoki.db.DatabaseError

::: hinoki.db.UniqueViolation

::: hinoki.db.ForeignKeyViolation

::: hinoki.db.NotNullViolation

::: hinoki.db.CheckViolation
