---
status: implemented
related:
  - settings.md
  - modules.md
  - migrations.md
  - models.md
---

# Configure a database

Hinoki's ORM supports PostgreSQL, SQLite and MySQL. SQLite is the default for
local development. The backend is selected by the database URL; an application
uses the same `Model` and query APIs across these backends where their SQL
features overlap.

## Configure the application

Declare `DatabaseSettings` on your app settings and install `DatabaseModule`.
The module opens one bounded pool per worker during startup and closes it during
shutdown. Importing models or routes does not open a connection.

```python
from hinoki import Hinoki, settings
from hinoki.db import DatabaseModule


class BlogSettings(settings.AppSettings, prefix="BLOG_", dotenv=".env"):
    database: settings.DatabaseSettings


app = Hinoki(settings=BlogSettings, modules=[DatabaseModule])
```

`DatabaseSettings.url` defaults to `sqlite://./.hinoki/local.sqlite3`, so no
database URL is needed for local development. You can override it with an
environment variable (the URL is a secret setting; avoid committing it):

```sh
# Optional override; SQLite is selected automatically when this is omitted.
export BLOG_DATABASE__URL='sqlite://./.hinoki/local.sqlite3'

# PostgreSQL
export BLOG_DATABASE__URL='postgresql://blog:password@db.example.test/blog'

# MySQL
export BLOG_DATABASE__URL='mysql://blog:password@db.example.test/blog'

# Pool capacity is per worker. Omit this to use the default of 10.
export BLOG_DATABASE__MAX_SIZE=10
```

Use a secret manager or a `BLOG_DATABASE__URL_FILE` variable for deployment
credentials. Hinoki redacts the URL in settings output and connection errors.
Avoid placing a credential-bearing URL in shell history or a process argument.

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
| SQLite | `sqlite://./.hinoki/local.sqlite3` | Recommended local default. SQLite allows one writer at a time; Hinoki uses bounded connections and a busy timeout. Relative paths are resolved from the application working directory. |
| PostgreSQL | `postgresql://user:password@host/database` | Server database with native UUID, timestamp, JSON and byte types. TLS options and verification behavior are described in the [database reference](reference/database.md). |
| MySQL | `mysql://user:password@host/database` | Requires TLS by default, with certificate and host verification. The system trust store must trust the server certificate. `require_ssl=false` disables encryption and is only for an isolated disposable local server. MySQL uses InnoDB and MySQL-native JSON; `RETURNING` is unavailable. |

MariaDB, SQL Server and Oracle are not supported by this release. MySQL's URL
scheme does not imply MariaDB compatibility.

## Create and apply migrations

Migrations use the same selected URL. With no explicit URL, the CLI uses the
local SQLite database. For another backend, export the URL without passing it
as a process argument:

```sh
export HINOKI_DATABASE_URL="$BLOG_DATABASE__URL"
hinoki migrations --dir blog/migrations plan --models blog.models --name initial
hinoki migrations --dir blog/migrations status
hinoki migrations --dir blog/migrations sql 0001-initial
hinoki migrations --dir blog/migrations apply
```

Review generated SQL and state before applying them. Use a disposable database
first. Hinoki does not run migrations automatically at app startup.

Migration behavior differs by backend:

* **SQLite** rebuilds a table when a safe direct alteration is unavailable and
  checks foreign keys before committing the migration.
* **PostgreSQL** applies migrations in a transaction.
* **MySQL** implicitly commits DDL. If a migration fails, earlier statements
  may already have changed the schema; Hinoki reports that partial application
  is possible. Inspect schema and migration status before retrying, and retain
  a recoverable backup.

See the [migration guide](migrations.md) for destructive-change review and
planning details, and the [database API reference](reference/database.md) for
types, raw SQL placeholders and backend limits.
