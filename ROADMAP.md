# Hinoki roadmap

Hinoki is pre-alpha. This is a short public view of the work; implementation status may change as the project develops.

## Available today

- Typed Python HTTP APIs with optional architecture rules and declared errors.
- The Rust extension currently provides path matching, selected request parsing, schema validation/serialization, cryptography, and PostgreSQL, SQLite, MySQL and MariaDB ORM execution. The ASGI adapter and application handlers run in Python, except that, on Kiso (the default), an `async def` handler whose only awaits are ORM reads and stock writes the framework can prove safe (guarded, body and `atomic` routes included) is driven from Rust without an asyncio task; eligible public generated `create`, `update` and `delete` actions run their transaction in Rust. SQLite is the local-development default.
- Sync and async handlers, settings, modules, migrations, OpenAPI, MCP tools, model lifecycle hooks, `bulk_*`/`abulk_*` writes, atomic writes (`atomic=True` on actions, `db.atomic` as a block or decorator in application code), response checks against annotations, route rate limit policies, and an optional admin with four themes and its own users.

See the [documentation](https://hinoki.caspel.dev/) and runnable [examples](examples/) for current details. No performance claims are made without comparable retained measurements.

## Active direction

- Improve the developer and documentation experience while keeping the public API small. `crud(...)` (function and class decorator, with `CrudAccess`) is the CRUD declaration, and its class form carries the `get_queryset`, `to_schema`, `written_queryset`, `store`, `change` and `remove` hooks.
- Bring `Schema` field types close to what Pydantic accepts, each step on both the Python and the Rust lane with OpenAPI, rules and documentation. Available: `dict[str, X]`, `date`, `time`, `timedelta`, `Decimal`, `bytes`, `Enum`, `StrEnum`, `IntEnum`, `Literal`, `set`, `frozenset`, `tuple`, plain unions and `Any`; `multiple_of` on `int` and `Decimal`; IP addresses and networks (also as `Model` columns) and the `Email`, `Url` and `Slug` annotations; `SecretStr` and `Path`; `TypedDict` and dataclass fields; `Field(strip=True)`, `Field(lower=True)` and `Field(upper=True)` for `str` fields; `pattern` on `bytes`; `Field(description=...)`, `examples` and `deprecated` for the OpenAPI document; `Schema.replace()` for a validated copy; `date`, `time`, `timedelta`, `Decimal` and `Enum` as `Model` columns; `@computed_field`; generic schemas (`class Envelope[T](Schema)` used as `Envelope[User]`); `@before_validate` to reshape raw input before validation (see [schemas](docs/schemas.md#reshaping-input-with-before_validate)); `Decimal` model columns as `numeric` on PostgreSQL and `decimal(65,30)` on MySQL/MariaDB, filterable and orderable there (equality only on SQLite). Strings and dict keys containing NUL are refused with a 422. Custom validators (`field_check`, `field_normalize`, `object_check`) already exist and run on the Python lane by design (see [validation](docs/schemas.md#built-in-and-custom-validation)).
- Validate the server integration, admin styles and generated typing contracts across real usage.
- Publish only after the site and release process have been reviewed.

## Planned for a later release

These are directions, not commitments, and none of them is available today.

- Moving more of the request path into the Rust core, before new features.
- Cursor pagination.
- WebSockets and streaming responses.
- Per-route CORS policies.
- Native response compression, Prometheus metrics and reload on `SIGHUP` in Kiso.
- Distributing as a single package, `hinoki-api` (import and CLI stay `hinoki`), with the admin remaining an optional extra of that package rather than a separate distribution.

This roadmap is intentionally curated; detailed queue notes and owner decisions are maintained locally and are not part of the public repository.
