# Hinoki roadmap

Hinoki is pre-alpha. This is a short public view of the work; implementation status may change as the project develops.

## Available today

- Typed Python HTTP APIs with optional architecture rules and declared errors.
- The Rust extension currently provides path matching, selected request parsing, schema validation/serialization, cryptography, and PostgreSQL, SQLite, MySQL and MariaDB ORM execution. The ASGI adapter and application handlers run in Python. SQLite is the local-development default.
- Sync and async handlers, settings, modules, migrations, OpenAPI, MCP tools, model lifecycle hooks, atomic writes (`atomic=True` on actions, `db.atomic` as a block or decorator in application code), response checks against annotations, route rate limit policies, and an optional admin with four themes and its own users.

See the [documentation](https://caspel26.github.io/hinoki/) and runnable [examples](examples/) for current details. No performance claims are made without comparable retained measurements.

## Active direction

- Improve the developer and documentation experience while keeping the public API small.
- Bring `Schema` field types close to what Pydantic accepts, each step on both the Python and the Rust lane with OpenAPI, rules and documentation. Available: `dict[str, X]`, `date`, `time`, `timedelta`, `Decimal`, `bytes`, `Enum`, `StrEnum`, `IntEnum` and `Literal`. Next, in order: `set`, `frozenset` and `tuple`; plain unions, `Any`, `TypedDict` and dataclass fields; more constraints (`multiple_of`, strict mode); IP addresses and networks, URLs, `SecretStr`, `Path` and a syntactic email check; validators and computed fields where the Rust lane can stay consistent.
- Validate the server integration, admin styles and generated typing contracts across real usage.
- Publish only after the site and release process have been reviewed.

This roadmap is intentionally curated; detailed queue notes and owner decisions are maintained locally and are not part of the public repository.
