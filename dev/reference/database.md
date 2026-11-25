# Database API

For setup, application settings, URL examples and migration workflow, see
[configure a database](https://hinoki.caspel.dev/dev/databases.md).

The ORM supports PostgreSQL, SQLite, MySQL and MariaDB. SQLite is the
local-development default; configure an explicit URL to select a server
backend. Raw SQL uses the selected driver's placeholders: PostgreSQL uses
``$1``, while SQLite, MySQL and MariaDB use ``?``. ORM-generated queries select
the dialect automatically.

## Configure a backend

Database URLs are secrets, so configure them through the environment rather
than TOML. For local development, leave the URL unset to use the default
SQLite file. The migration commands read the application's `database` setting
too; `HINOKI_DATABASE_URL` overrides it for them ([which
database](https://hinoki.caspel.dev/dev/migrations.md#which-database)). TOML may configure
non-secret pool options, for example:

```toml
[database]
max_size = 8
```

For PostgreSQL, use a driver URL such as ``postgresql://user:password@host/db``.
For MySQL, use ``mysql://user:password@host/db``. MySQL connections require TLS
by default; the driver verifies the server certificate and hostname. Configure
the system trust store to trust your server certificate. ``require_ssl=false``
is only suitable for isolated local testing and disables transport encryption.
Do not put production credentials in source-controlled settings files.
For MariaDB, use ``mariadb://user:password@host/db``; it is selected and tested
as a distinct backend even though it uses the MySQL wire driver and SQL dialect.

## Backend behavior and limits

| Backend | Status | Notes |
| --- | --- | --- |
| SQLite | Supported | Default for local development. SQLite permits one writer at a time. Table changes that SQLite cannot express directly use a table rebuild. |
| PostgreSQL | Supported | Uses PostgreSQL-native UUID, timestamp, JSON and byte types. |
| MySQL | Supported | Uses InnoDB tables, binary UUIDs and MySQL-native JSON. MySQL DDL implicitly commits, so a failed migration can leave earlier DDL applied; inspect migration status and table state before retrying. `RETURNING` is not supported by the compiler; ORM create/update operations use follow-up reads. TLS is required by default. |
| MariaDB | Supported | Tested independently on MariaDB 11.4. Its `JSON` alias is text, so typed ORM `dict`/`list` fields are decoded from the model declaration. Low-level `Database.fetch()` returns JSON values as bytes because arbitrary text columns cannot be safely inferred as JSON. DDL can implicitly commit; TLS is required by default. |

SQL Server and Oracle are not currently supported. Do not assume that MySQL
compatibility implies MariaDB compatibility. Hinoki does not silently roll
back MySQL-family DDL: review generated migrations and keep a recoverable
backup before applying migrations.

Datetime values stored in MySQL ``DATETIME`` columns use Hinoki's UTC-naive
storage convention; values returned by the ORM are interpreted as UTC. MySQL
pool capacity and acquisition wait are bounded by the database pool settings.

Raw SQL must use the placeholders for the configured backend. Prefer ORM query
methods when the same code should work across backends; raw SQL and migration
operations may need backend-specific SQL.

- `hinoki.db.Database(engine: 'core.Database') -> 'None'`: A connection pool for one configured database backend. [reference](https://hinoki.caspel.dev/dev/reference/database/#hinoki.db.Database)

- `hinoki.db.atomic(func: 'Callable[..., Any] | None' = None, /) -> 'Any'`: ``async with db.atomic():``, ``with db.atomic():``, ``@db.atomic`` or ``@db.atomic()``: run a block or a function, ``async def`` or plain ``def``, atomically…. [reference](https://hinoki.caspel.dev/dev/reference/database/#hinoki.db.atomic)

- `hinoki.db.Atomic()`: ``db.atomic()``: an atomic scope on the current database, as a block or as a decorator. [reference](https://hinoki.caspel.dev/dev/reference/database/#hinoki.db.Atomic)

- `hinoki.db.DatabaseModule(url: 'str', *, max_size: 'int' = 10, wait: 'float' = 5.0) -> 'None'`: Opens a ``Database`` for the worker and provides it to other modules. [reference](https://hinoki.caspel.dev/dev/reference/database/#hinoki.db.DatabaseModule)

- `hinoki.db.DatabaseError`: Common base class for all non-exit exceptions. [reference](https://hinoki.caspel.dev/dev/reference/database/#hinoki.db.DatabaseError)

- `hinoki.db.UniqueViolation`: Common base class for all non-exit exceptions. [reference](https://hinoki.caspel.dev/dev/reference/database/#hinoki.db.UniqueViolation)

- `hinoki.db.ForeignKeyViolation`: Common base class for all non-exit exceptions. [reference](https://hinoki.caspel.dev/dev/reference/database/#hinoki.db.ForeignKeyViolation)

- `hinoki.db.NotNullViolation`: Common base class for all non-exit exceptions. [reference](https://hinoki.caspel.dev/dev/reference/database/#hinoki.db.NotNullViolation)

- `hinoki.db.CheckViolation`: Common base class for all non-exit exceptions. [reference](https://hinoki.caspel.dev/dev/reference/database/#hinoki.db.CheckViolation)
