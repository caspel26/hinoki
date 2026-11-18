---
status: implemented
related:
  - settings.md
  - modules.md
  - migrations.md
  - models.md
---

# Configure a database

Hinoki's ORM supports PostgreSQL, SQLite, MySQL and MariaDB. SQLite is the
default for local development. The backend is selected by the database URL; an
application uses the same `Model` and query APIs across these backends where
their SQL features overlap.

## Configure the application

Declare a `database: DatabaseSettings` field on your app settings. Hinoki then
opens one bounded pool per worker during startup and closes it during shutdown;
importing models or routes does not open a connection, and no `modules=` entry
is needed.

```python
from hinoki import Hinoki, settings


class BlogSettings(
    settings.AppSettings, prefix="BLOG_", dotenv=".env", toml="hinoki.toml"
):
    database: settings.DatabaseSettings


app = Hinoki(settings=BlogSettings)
```

With nothing configured, the database is local SQLite at
`.hinoki/local.sqlite3`. Configure a server database either with separate
connection fields or with one URL; setting both is an error when settings load.

### Connection fields

`engine` selects the backend with the `settings.DatabaseEngine` values
`sqlite`, `postgresql`, `mysql` and `mariadb`. `name` is the database name, or
the file path for SQLite. `options` are `key=value` URL query pairs such as
PostgreSQL's `sslmode`. `host` defaults to `localhost`; the password is a secret
setting.

```toml
# hinoki.toml, or the file named by the settings class's toml=
[database]
engine = "postgresql"
name = "blog"
user = "blog"
host = "db.example.test"
port = 5432
options = ["sslmode=verify-full", "sslrootcert=/run/secrets/db-ca.pem"]
max_size = 10            # per worker; the default is 10
```

```sh
export BLOG_DATABASE__PASSWORD='…'     # or BLOG_DATABASE__PASSWORD_FILE
```

Hinoki builds the connection URL from these fields, percent-encoding the user,
password and name, and keeps it in `DatabaseSettings.url`. SQLite takes only
`name`.

For PostgreSQL, `host` may be a Unix-socket directory such as
`/var/run/postgresql`; it is encoded in the generated URL. Other engines require
a host name or address. Environment values for `options` use the settings
sequence syntax, with commas separating `key=value` pairs. If an option value
itself contains a comma, put it in the TOML array or percent-encode it in a URL.
Options cannot specify credentials or override `user`, `host`, `port` or database
name; use their dedicated fields instead. The password always belongs in the
secret `password` setting or in a credential-bearing URL supplied as a secret.

### One URL

```sh
export BLOG_DATABASE__URL='postgresql://blog:password@db.example.test/blog'
export BLOG_DATABASE__URL='postgresql://blog:password@db.example.test/blog?sslmode=verify-full&sslrootcert=%2Frun%2Fsecrets%2Fdb-ca.pem'
export BLOG_DATABASE__URL='mysql://blog:password@db.example.test/blog'
export BLOG_DATABASE__URL='sqlite://./.hinoki/local.sqlite3'
```

A `DatabaseModule` listed in `modules=`, or installed with `app.install(...)`,
replaces the one Hinoki would create from the settings, for example to pass a
URL chosen at runtime: `DatabaseModule(url, max_size=4)`.

Use a secret manager or a `BLOG_DATABASE__URL_FILE` / `BLOG_DATABASE__PASSWORD_FILE`
variable for deployment credentials. Hinoki redacts the URL in settings output and connection errors.
Avoid placing a credential-bearing URL in shell history or a process argument.
Database authentication uses the username and password; in a hand-written URL,
percent-encode reserved characters in either component. SQLite files are protected by the
operating-system account and filesystem permissions, not by database credentials.

For non-secret pool settings, TOML works as well. Keep `url` in an environment
variable or secret file; `settings.Secret` values are not read from TOML.

```toml
[database]
max_size = 12
```

See [settings](settings.md) for source precedence and validation, and
[application modules](modules.md) for resource ownership and access to the
provided `Database` instance.

## Backend URLs and behavior

| Backend | URL example | Use and constraints |
| --- | --- | --- |
| SQLite | `sqlite://./.hinoki/local.sqlite3` | Supported; recommended local default. No network authentication or TLS. SQLite allows one writer at a time; Hinoki uses bounded connections and a five-second busy timeout. Relative paths are resolved from the application working directory. |
| PostgreSQL | `postgresql://user:password@host/database` | Supported. Uses PostgreSQL username/password authentication and native UUID, timestamp, JSON and byte types. `sslmode` defaults to `prefer`; use `sslmode=verify-full` for remote databases. `sslrootcert` adds a PEM CA certificate to the system roots. Hinoki accepts `disable`, `prefer`, `require`, `verify-ca` and `verify-full`; the last three all require TLS and certificate verification in Hinoki. |
| MySQL | `mysql://user:password@host/database` | Supported. Uses username/password authentication and requires TLS by default, with certificate and host verification. The system trust store must trust the server certificate. `require_ssl=false` disables encryption and is only for an isolated disposable local server. MySQL uses InnoDB and MySQL-native JSON; `RETURNING` is unavailable. |
| MariaDB | `mariadb://user:password@host/database` | Supported and tested separately on MariaDB 11.4. Uses the MySQL wire driver and SQL dialect. MariaDB's `JSON` alias is text; typed ORM `dict`/`list` fields are decoded by their model declaration. Low-level `Database.fetch()` returns MariaDB JSON values as bytes because server metadata cannot safely distinguish JSON from arbitrary text. TLS is required by default. |

SQL Server and Oracle are not supported by this release. MySQL and MariaDB are
separate URL/backend identities and each has its own real-server conformance
job; MySQL's URL scheme does not imply MariaDB compatibility.

Any other URL scheme, such as `oracle://` or `mssql://`, fails when the pool is
created with `DatabaseError: unsupported database URL scheme`; the error does not
repeat the URL, so credentials in it are not logged.

## Create and apply migrations

Migrations use the same selected URL. With no explicit URL, the CLI uses the
local SQLite database. For another backend, export the URL without passing it
as a process argument:

```sh
cd examples
export HINOKI_DATABASE_URL="$BLOG_DATABASE__URL"
uv run --project .. hinoki migrations plan --app blog
uv run --project .. hinoki migrations status
uv run --project .. hinoki migrations sql --app blog 0001-initial
uv run --project .. hinoki migrations sql --app blog 0002-accounts
uv run --project .. hinoki migrations sql --app blog 0003-index-blog-posts-author-id
uv run --project .. hinoki migrations apply --app blog
```

Review generated SQL and state before applying them. Use a disposable database
first. Hinoki does not run migrations automatically at app startup.

On a fresh local checkout, `hinoki migrations ... apply` uses the default
SQLite URL when `HINOKI_DATABASE_URL` is unset. Opening that URL creates
`.hinoki/local.sqlite3` if it does not exist; applying creates the migration
ledger and runs the pending SQL files. As with Django's separate
`makemigrations` and `migrate` steps, first run `plan` to generate migration
files from model changes, then run `apply` to create the tables. `status` also
opens the selected database and creates its ledger table if needed.

Migration behavior differs by backend:

* **SQLite** rebuilds a table when a safe direct alteration is unavailable and
  checks foreign keys before committing the migration. Keep the database file
  and its backups on storage with permissions limited to the application user.
* **PostgreSQL** applies migrations in a transaction.
* **MySQL** implicitly commits DDL. If a migration fails, earlier statements
  may already have changed the schema; Hinoki reports that partial application
  is possible. Inspect schema and migration status before retrying, and retain
  a recoverable backup.
* **MariaDB** uses the MySQL wire protocol, but is a separate backend and is
  tested against its own service. Its JSON type is a text alias: `Model`
  fields annotated `dict` or `list` are decoded by the typed ORM, while the
  low-level `Database.fetch()` API returns JSON column values as bytes.

Hinoki creates MySQL and MariaDB foreign keys as table-level constraints for
consistent behavior across the two dialects. MySQL ignores inline `REFERENCES`
clauses; MariaDB behavior differs by server version. For schemas created by an
older Hinoki version, inspect `SHOW CREATE TABLE` before relying on referential
integrity or `on_delete`. A migration ledger entry alone does not establish
that MySQL created the constraint. Write a reviewed repair migration where a
constraint is missing; Hinoki does not silently rewrite applied migrations.

See the [migration guide](migrations.md) for destructive-change review and
planning details, and the [database API reference](reference/database.md) for
types, raw SQL placeholders and backend limits.
