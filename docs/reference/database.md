# Database API

The ORM supports PostgreSQL, SQLite and MySQL. SQLite is the local-development
default; configure an explicit URL for PostgreSQL or MySQL deployments. Raw SQL
uses the selected driver's placeholders: PostgreSQL uses ``$1``, while SQLite
and MySQL use ``?``. ORM-generated queries select the dialect automatically.

## Configure a backend

```toml
[database]
url = "sqlite://./local.db"
```

For PostgreSQL, use a driver URL such as ``postgresql://user:password@host/db``.
For MySQL, use ``mysql://user:password@host/db``. MySQL connections require TLS
by default; the driver verifies the server certificate and hostname. Configure
the system trust store to trust your server certificate. ``require_ssl=false``
is only suitable for isolated local testing and disables transport encryption.
Do not put production credentials in source-controlled settings files.

## Backend behavior and limits

| Backend | Status | Notes |
| --- | --- | --- |
| SQLite | Supported | Default for local development. SQLite permits one writer at a time. Schema changes that SQLite cannot express directly use a table rebuild. |
| PostgreSQL | Supported | Uses PostgreSQL-native UUID, timestamp, JSON and byte types. |
| MySQL | Supported | Uses InnoDB tables, binary UUIDs and MySQL-native JSON. MySQL DDL implicitly commits, so a failed migration can leave earlier DDL applied; inspect migration status and database schema before retrying. `RETURNING` is not supported by the compiler; ORM create/update operations use follow-up reads. TLS is required by default. |

MariaDB, SQL Server and Oracle are not currently supported. Do not assume that
MySQL compatibility implies MariaDB compatibility. Hinoki does not silently
roll back MySQL DDL: review generated migrations and keep a recoverable backup
before applying schema changes.

Datetime values stored in MySQL ``DATETIME`` columns use Hinoki's UTC-naive
storage convention; values returned by the ORM are interpreted as UTC. MySQL
pool capacity and acquisition wait are bounded by the database pool settings.

Raw SQL must use the placeholders for the configured backend. Prefer ORM query
methods when the same code should work across backends; raw SQL and migration
operations may need backend-specific SQL.

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
