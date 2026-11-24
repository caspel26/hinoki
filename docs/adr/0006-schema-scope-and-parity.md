# ADR 0006 — `Schema` scope, Python/Rust parity and non-goals

Status: Accepted · 2026-11-23

**Context.** Hinoki owns its `Schema` layer rather than depending on an external validation library. Validation and serialization run across two lanes—Python (`hinoki.schema`) and Rust (`crates/bindings/src/schema`, including the GIL-free JSON path)—and drive OpenAPI document generation, architecture rules and `Model` conversion. Expanding `Schema` to cover every niche type or convenience hook of a general-purpose validation library multiplies parity work across both lanes, OpenAPI schema objects and differential tests without improving the HTTP/JSON boundary.

**Decision.** `Schema` stays bounded to typed data crossing the API and ORM boundary, with explicit non-goals and a parity requirement for future additions:

- **In scope.** Standard JSON, HTTP and ORM types: scalars (`str`, `int`, `float`, `bool`, `bytes`, `Decimal`), `UUID`, `date`, `time`, `datetime`, `timedelta`, IP addresses and networks, `Email`, `Url`, `Slug`, `Secret`, `Path`, `Enum`/`StrEnum`/`IntEnum`, `Literal`, collections (`list`, `set`, `frozenset`, `tuple`, `dict[str, T]`), plain and discriminated unions, `TypedDict` and dataclasses, `Unset`/`UNSET`, generic schemas, `@computed_field`, `@before_validate`, `Field(...)` constraints and string normalization, and custom validators (`@field_check`, `@field_normalize`, `@object_check`) on the Python lane.
- **Non-goals (out of scope).** Arbitrary Python types without a wire representation (`arbitrary_types_allowed`), hidden permissive or lossy coercion on JSON input (only explicit text sources convert text to typed fields), a third-party validator plugin ecosystem, stateful serializers or serializers that perform implicit I/O, and mutable `Schema` instances (use `replace()` for validated copies).
- **Parity rule (definition of done).** Any new `Schema` field type or constraint lands in one cohesive change with: (1) Python and Rust lane support—or an explicit rationale when a feature is Python-lane-only, such as custom validator hooks; (2) OpenAPI document generation (JSON Schema output); (3) differential tests verifying identical values and errors across both lanes (`tests/schemadifftest.py`); and (4) documentation in `docs/schemas.md` and the related schema guides.

**Consequences.** The `Schema` surface stays stable and maintainable across both lanes ahead of `0.1`. Applications that need non-JSON domain types, legacy payload reshaping or cross-record rules use `@before_validate`, `@field_check`/`@object_check`, or plain application objects outside the wire `Schema`.

**Alternatives.** Open-ended parity with general-purpose validation libraries (unbounded maintenance across two engines and OpenAPI); Python-only validation (contradicts the Rust core boundary for request parsing and serialization); disallowing Python hooks (too restrictive for domain rules).

**Revisit when** an excluded type recurs across real applications and has an unambiguous JSON wire representation and OpenAPI schema object mapping.
