---
status: implemented
related:
  - settings.md
  - migrations.md
  - models.md
  - modules.md
---

# Configure a database

Hinoki's ORM runs on SQLite, PostgreSQL, MySQL and MariaDB. The database URL
selects the backend; models and queries stay the same wherever the backends'
SQL features overlap. With nothing configured, the database is local SQLite at
`.hinoki/local.sqlite3`.

## Configure the application

Declare a `database` field on the project settings. `hinoki new` adds it when
you choose a database.

```python
# notes/settings.py
from hinoki import settings


class ProjectSettings(
    settings.AppSettings, prefix="NOTES_", dotenv=".env", toml="settings.toml"
):
    database: settings.DatabaseSettings
```

Each worker opens one bounded pool during startup and closes it during
shutdown. Importing models or routes never opens a connection, and no
`modules=` entry is needed.

Configure a server database with **connection fields** or with **one URL**.
Setting both is an error when settings load.

## Connection fields

Put non-secret fields in the TOML file named by `toml=`. Keep the settings in
their own file: `hinoki.toml` is the project manifest, and its tables are not
settings.

=== "PostgreSQL"

    ```toml
    # settings.toml
    [database]
    engine = "postgresql"
    name = "notes"
    user = "notes"
    host = "db.example.test"
    port = 5432
    options = ["sslmode=verify-full", "sslrootcert=/run/secrets/db-ca.pem"]
    max_size = 10            # per worker
    ```

=== "MySQL"

    ```toml
    # settings.toml
    [database]
    engine = "mysql"
    name = "notes"
    user = "notes"
    host = "db.example.test"
    ```

=== "MariaDB"

    ```toml
    # settings.toml
    [database]
    engine = "mariadb"
    name = "notes"
    user = "notes"
    host = "db.example.test"
    ```

=== "SQLite"

    ```toml
    # settings.toml
    [database]
    engine = "sqlite"
    name = ".hinoki/local.sqlite3"
    ```

The password is a secret setting. It never comes from TOML:

```sh
export NOTES_DATABASE__PASSWORD_FILE=/run/secrets/db-password
```

| Field | Meaning | Default |
| --- | --- | --- |
| `engine` | `sqlite`, `postgresql`, `mysql` or `mariadb` (`settings.DatabaseEngine`) | – |
| `name` | database name, or the file path for SQLite | – |
| `user`, `password` | credentials; `password` is secret (`*_PASSWORD` or `*_PASSWORD_FILE`) | – |
| `host`, `port` | server address; for PostgreSQL `host` may be a Unix-socket directory such as `/var/run/postgresql` | `localhost`, backend port |
| `options` | `key=value` URL query pairs, such as `sslmode` | `()` |
| `max_size` | connections per worker | `10` |

Hinoki builds `DatabaseSettings.url` from these fields and percent-encodes the
user, password and name. SQLite takes only `name`. `options` cannot carry
credentials or override `user`, `host`, `port` or the name. In an environment
variable, `options` uses the settings sequence syntax (commas between pairs);
a value that itself contains a comma belongs in the TOML array.

## One URL

```sh
export NOTES_DATABASE__URL_FILE=/run/secrets/database-url
```

| Backend | URL |
| --- | --- |
| SQLite | `sqlite://./.hinoki/local.sqlite3` |
| PostgreSQL | `postgresql://notes:password@db.example.test/notes?sslmode=verify-full` |
| MySQL | `mysql://notes:password@db.example.test/notes` |
| MariaDB | `mariadb://notes:password@db.example.test/notes` |

Percent-encode reserved characters in the user name and password of a
hand-written URL. Prefer `NOTES_DATABASE__URL_FILE` or a secret manager over a
URL in shell history or a process argument. Hinoki redacts the URL in settings
output and connection errors.

!!! warning "Credentials"
    `settings.Secret` values, `url` and `password` included, are never read from
    TOML. SQLite has no database credentials: protect the file and its backups
    with operating-system permissions.

## Backends

| Backend | Security defaults | Limits |
| --- | --- | --- |
| SQLite | no network, no TLS | one writer at a time; bounded connections and a five-second busy timeout; a relative path resolves from the application's working directory |
| PostgreSQL | `sslmode=prefer`; use `verify-full` for remote servers; `sslrootcert` adds a PEM CA to the system roots. `require`, `verify-ca` and `verify-full` all verify the certificate | native UUID, timestamp, JSON and byte types |
| MySQL | TLS required, certificate and host verified against the system trust store; `require_ssl=false` only for an isolated disposable local server | InnoDB, native JSON; no `RETURNING` |
| MariaDB | TLS required by default | separate backend, tested on MariaDB 11.4 with its own conformance job. `JSON` is a text alias: typed `dict`/`list` fields decode by declaration; low-level `Database.fetch()` returns JSON values as bytes |

SQL Server and Oracle are not supported. Any other scheme, such as `oracle://`
or `mssql://`, fails when the pool is created with
`DatabaseError: unsupported database URL scheme`; the message does not repeat
the URL.

## Replace the database module

A `DatabaseModule` in `modules=`, or installed with `app.install(...)`, replaces
the one Hinoki builds from the settings, for example to use a URL chosen at
runtime:

```python
# notes/main.py (excerpt)
from hinoki.db import DatabaseModule

app.install(DatabaseModule(url, max_size=4))
```

## Migrations use their own URL

`hinoki migrations` does not read the application's settings. It uses
`--database`, then `HINOKI_DATABASE_URL`, then the local SQLite default
anchored beside `hinoki.toml`. Point it at the application's database
explicitly:

```sh
export HINOKI_DATABASE_URL="$NOTES_DATABASE__URL"
hinoki migrations status
hinoki migrations apply
```

Hinoki never runs migrations at startup. See [migrations](migrations.md) for
planning, review and per-backend behaviour.

## See also

- [Settings](settings.md): source precedence, secrets and validation
- [Application modules](modules.md): resource ownership and the `Database` instance
- [Database API reference](reference/database.md): raw SQL, placeholders and backend limits
