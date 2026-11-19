# Hinoki roadmap

Hinoki is pre-alpha. This is a short public view of the work; implementation status may change as the project develops.

## Available today

- Typed Python HTTP APIs with optional architecture rules and declared errors.
- The Rust extension currently provides path matching, selected request parsing, schema validation/serialization, cryptography, and PostgreSQL, SQLite, MySQL and MariaDB ORM execution. The ASGI adapter and application handlers run in Python. SQLite is the local-development default.
- Sync and async handlers, settings, modules, migrations, OpenAPI, MCP tools, model lifecycle hooks, atomic writes (`atomic=True` on actions, `db.atomic` as a block or decorator in application code), response checks against annotations, route rate limit policies, and an optional admin with four themes and its own users.

See the [documentation](https://caspel26.github.io/hinoki/) and runnable [examples](examples/) for current details. No performance claims are made without comparable retained measurements.

## Active direction

- Improve the developer and documentation experience while keeping the public API small. `crud(...)` (function and class decorator, with `CrudAccess`) is the CRUD declaration; the `CrudAPI` base class remains available for the `store`/`change`/`remove` hooks and for compatibility.
- Bring `Schema` field types close to what Pydantic accepts, each step on both the Python and the Rust lane with OpenAPI, rules and documentation. Available: `dict[str, X]`, `date`, `time`, `timedelta`, `Decimal`, `bytes`, `Enum`, `StrEnum`, `IntEnum`, `Literal`, `set`, `frozenset`, `tuple`, plain unions and `Any`; `multiple_of` on `int` and `Decimal`; IP addresses and networks (also as `Model` columns) and the `Email`, `Url` and `Slug` annotations; `SecretStr` and `Path`; `TypedDict` and dataclass fields; `date`, `time`, `timedelta`, `Decimal` and `Enum` as `Model` columns. Next, in order: more constraints (strict mode, string normalisation, `pattern` on `bytes`); computed fields, which change the native JSON encoder, `dump()`, projections and the OpenAPI response schema together. Custom validators (`field_check`, `field_normalize`, `object_check`) already exist and run on the Python lane by design (see [validation](docs/schemas.md#built-in-and-custom-validation)).
- Validate the server integration, admin styles and generated typing contracts across real usage.
- Publish only after the site and release process have been reviewed.

## Planned for a later release

These are directions, not commitments, and none of them is available today.

- Moving more of the request path into the Rust core, before new features.
- Cursor pagination.
- WebSockets and streaming responses.
- Per-route CORS policies.
- Distributing as a single package, `hinoki-api` (import and CLI stay `hinoki`), with the admin remaining an optional extra of that package rather than a separate distribution.

This roadmap is intentionally curated; detailed queue notes and owner decisions are maintained locally and are not part of the public repository.
