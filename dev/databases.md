# Configure a database

Hinoki's ORM runs on SQLite, PostgreSQL, MySQL and MariaDB. The database URL
selects the backend; models and queries stay the same wherever the backends'
SQL features overlap. With nothing configured, the database is local SQLite at
`.hinoki/local.sqlite3`.

## Set up a database in four steps

1. **Declare it.** `hinoki new notes --database postgresql` does this for you;
   otherwise add the field to your settings class. Name a TOML file only if
   you will use one (step 2): the file named by `toml=` must exist.

    ```python
    # notes/settings.py
    class ProjectSettings(settings.AppSettings, prefix="NOTES_", dotenv=".env"):
        database: settings.DatabaseSettings
    ```

2. **Populate it.** With nothing set, `DatabaseSettings` points at a local
   SQLite file, `.hinoki/local.sqlite3`. To use another database, pick one
   source:

    | Source | Use it for | Example |
    | --- | --- | --- |
    | Nothing | local development on SQLite | – |
    | `.env` | one URL on your machine (`hinoki new --database postgresql` writes a `.env.example` to copy; keep `.env` out of git) | `NOTES_DATABASE__URL=postgresql://notes:secret@localhost:5432/notes` |
    | Environment | containers and CI | `export NOTES_DATABASE__URL_FILE=/run/secrets/database-url` |
    | `settings.toml` | non-secret fields (`engine`, `host`, `name`, `user`), with the password from the environment; add `toml="settings.toml"` to the class | [connection fields](https://hinoki.caspel.dev/dev/databases.md#connection-fields) |

    The prefix (`NOTES_`) comes from the settings class, and `__` separates the
    group from the field. Until you set one of them, a project created with
    `--database postgresql` still uses the local SQLite file, and so do its
    migrations.

3. **Check it.** `hinoki settings`, run from the project root, shows each
   value with its source; the URL and the password are redacted:

    ```console
    $ hinoki settings | grep DATABASE__URL
    NOTES_DATABASE__URL                      .env         ***
    ```

    `hinoki migrations status` names the database it will use, and fails with
    a clear error if the server does not answer:

    ```console
    $ hinoki migrations status
    database: postgresql://***@127.0.0.1:5432/notes (from settings: ProjectSettings.database)
    ```

    `status` connects and records its migration ledger, so on SQLite it
    creates the database file.

4. **Create the tables.** Hinoki never creates tables or runs migrations at
   startup (it only creates the local SQLite file): run
   `hinoki migrations apply` once you have models ([migrations](https://hinoki.caspel.dev/dev/migrations.md)).

The rest of this page lists every field, the URL forms and each backend.

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
shutdown. The [Notes example's settings](https://github.com/caspel26/hinoki/blob/main/examples/notes/notes/settings.py)
are exactly this field with nothing else. Importing models or routes never opens a connection, and no
`modules=` entry is needed. The file named by `toml=` must exist, even if it
is empty: otherwise the settings fail to load with
`settings.toml: TOML file cannot be read or parsed`.

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
| SQLite | no network, no TLS | one writer at a time; bounded connections and a busy timeout of at least five seconds, extended when `Database.connect(wait=...)` is larger; a relative path resolves from the server's working directory (start it from the project root; migrations anchor it beside `hinoki.toml`) |
| PostgreSQL | `sslmode=prefer`; use `verify-full` for remote servers; `sslrootcert` adds a PEM CA to the system roots. `require`, `verify-ca` and `verify-full` all verify the certificate | native UUID, timestamp, JSON and byte types |
| MySQL | TLS required, certificate and host verified against the system trust store; `require_ssl=false` only for an isolated disposable local server | InnoDB, native JSON; no `RETURNING` |
| MariaDB | TLS required by default | separate backend, tested on MariaDB 11.4 with its own conformance job. `JSON` is a text alias: typed `dict`/`list` fields decode by declaration; low-level `Database.fetch()` returns JSON values as bytes |

SQL Server and Oracle are not supported. Any other scheme, such as `oracle://`
or `mssql://`, fails when the pool is created with
`DatabaseError: unsupported database URL scheme`; the message does not repeat
the URL. A supported scheme written without its slashes, such as
`sqlite:./app.db`, fails with `invalid database URL: the scheme needs ://`.

### PostgreSQL TLS

PostgreSQL URLs accept `sslmode=disable`, `prefer`, `require`, `verify-ca` or
`verify-full`, and `sslrootcert=/path/to/ca.pem`. `prefer` tries TLS first and
falls back to plaintext only if the server declines TLS; a certificate or
handshake error fails closed. All TLS connections verify the server
certificate chain and hostname: `require`, `verify-ca` and `verify-full` all
require TLS, and Hinoki's `verify-ca` also verifies the hostname. Trust uses
Mozilla WebPKI roots plus any PEM certificates in `sslrootcert` (at most
1 MiB), so `sslmode=require` needs a trusted certificate too. `sslmode=allow`
is unsupported. A URL to a remote host without `sslmode` logs a warning at
connect: `prefer` lets a network attacker strip TLS, so use `verify-full`.
Mount the CA file where each worker can read it.

Hinoki does not create the PostgreSQL role or database: create them once with
your usual tools before the first `hinoki migrations apply`.

### SQLite storage

SQLite uses bundled SQLite 3.35 or newer. UUID and timestamp columns use text
storage; JSON uses JSON text. `contains` matching is ASCII case-insensitive.
In-memory databases are not supported, because each pooled connection would
have an independent database. SQLite allows one writer at a time. Its internal
lock wait is the larger of five seconds and the pool's configured `wait`; the
pool acquisition wait itself remains bounded by `wait`:

```python
from hinoki.db import Database

database = await Database.connect("sqlite://./app.sqlite3", wait=10)
```

This gives SQLite writes up to ten seconds to acquire the writer lock. The
default `wait=5` keeps the existing five-second limit.

## Use the database from a script

A script, a one-off task or a test fixture opens its own pool with
`Database.connect` and makes it current with `use_database`, so model queries
find it. Run it from the project root so a relative SQLite path and `.env`
match the server's:

```python
# scripts/unpaid.py
import asyncio

from hinoki.db import Database
from hinoki.orm import use_database

from notes.apps.billing.models import Invoice
from notes.settings import ProjectSettings


async def main() -> None:
    settings = ProjectSettings.load()
    database = await Database.connect(
        settings.database.url.reveal(), max_size=settings.database.max_size
    )
    try:
        with use_database(database):
            print(await Invoice.filter(paid=False).acount())
    finally:
        database.close()


asyncio.run(main())
```

`Database.close()` is a plain call, not awaited. The script does not create
tables: run `hinoki migrations apply` against the same database first.

## Replace the database module

A `DatabaseModule` in `modules=`, or installed with `app.install(...)`, replaces
the one Hinoki builds from the settings, for example to use a URL chosen at
runtime:

```python
# notes/main.py (excerpt)
from hinoki.db import DatabaseModule

app.install(DatabaseModule(url, max_size=4))
```

The migration commands use this module's URL too, and print
`(from application: DatabaseModule)`. A database module of your own whose URL
Hinoki cannot read stops them; pass `--database` or `HINOKI_DATABASE_URL`.

## Migrations use the application's database

`hinoki migrations` reads the database of the project's application (its
`DatabaseModule`, or its `database` setting),
so the variables above configure both the server and the migration commands.
`--database` or `HINOKI_DATABASE_URL` override it for one run; with none of
them set, the local SQLite default is anchored beside `hinoki.toml`:

```sh
export NOTES_DATABASE__URL=postgresql://notes:secret@db.internal/notes
hinoki migrations status   # database: postgresql://***@db.internal/notes (from settings: ...)
hinoki migrations apply
```

Hinoki never runs migrations at startup. An application that declares no
`database` setting and installs no `DatabaseModule` has no database at run
time, although its migrations still fall back to the local SQLite file:
declare the field before you add models. See [which database](https://hinoki.caspel.dev/dev/migrations.md#which-database)
for the full order, and [migrations](https://hinoki.caspel.dev/dev/migrations.md) for planning, review and
per-backend behaviour.

## See also

- [Settings](https://hinoki.caspel.dev/dev/settings.md): source precedence, secrets and validation
- [Application modules](https://hinoki.caspel.dev/dev/modules.md): resource ownership and the `Database` instance
- [Database API reference](https://hinoki.caspel.dev/dev/reference/database.md): raw SQL, placeholders and backend limits
