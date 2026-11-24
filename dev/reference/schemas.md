# Schemas

- `hinoki.schema.Schema()`: Base class for schemas. [reference](https://hinoki.caspel.dev/dev/reference/schemas/#hinoki.schema.Schema)

- `hinoki.schemaconfig.SchemaConfig(*, extra: 'SchemaExtra' = <SchemaExtra.FORBID: 'forbid'>) -> None`: Immutable class-level Schema behavior configuration. [reference](https://hinoki.caspel.dev/dev/reference/schemas/#hinoki.schemaconfig.SchemaConfig)

- `hinoki.schemaconfig.SchemaExtra(*values)`: Policy for unknown string keys in decoded Schema input. [reference](https://hinoki.caspel.dev/dev/reference/schemas/#hinoki.schemaconfig.SchemaExtra)

- `hinoki.fields.Field(**options: 'Any') -> 'Any'`: Declare constraints (inside ``Annotated``) or a default factory. [reference](https://hinoki.caspel.dev/dev/reference/schemas/#hinoki.fields.Field)

- `hinoki.schema.Invalid(code: 'str', message: 'str', *, path: 'str' = '') -> 'None'`: Raised by a check to refuse a value: a stable ``code``, a readable ``message`` and optionally the ``path`` it concerns (default: the field the check is…. [reference](https://hinoki.caspel.dev/dev/reference/schemas/#hinoki.schema.Invalid)

- `hinoki.schema.Unset() -> 'Unset'`: Type of ``UNSET``: a field that was absent from the input (PATCH). [reference](https://hinoki.caspel.dev/dev/reference/schemas/#hinoki.schema.Unset)

- `hinoki.schema.field_check(*names: 'str') -> 'Callable[[F], F]'`: Mark a method as a check on one or more fields: ``def ok(self, value)``. [reference](https://hinoki.caspel.dev/dev/reference/schemas/#hinoki.schema.field_check)

- `hinoki.schema.field_normalize(name: 'str') -> 'Callable[[F], F]'`: Mark a typed field canonicalizer: ``def trim(self, value: str) -> str``. [reference](https://hinoki.caspel.dev/dev/reference/schemas/#hinoki.schema.field_normalize)

- `hinoki.schema.object_check(function: 'F') -> 'F'`: Mark a method as a check on the whole object: ``def ok(self)``. [reference](https://hinoki.caspel.dev/dev/reference/schemas/#hinoki.schema.object_check)

- `hinoki.schema.before_validate(function: 'F') -> 'F'`: Mark a classmethod or staticmethod that reshapes raw wire input: ``def prepare(cls, data: dict[str, Any]) -> dict[str, Any]``. [reference](https://hinoki.caspel.dev/dev/reference/schemas/#hinoki.schema.before_validate)

- `hinoki.computed.computed_field(...)`: Declare a read-only value derived from the other fields of a ``Schema``. [reference](https://hinoki.caspel.dev/dev/reference/schemas/#hinoki.computed.computed_field)

- `hinoki.fields.Secret(value: 'str') -> 'None'`: Text that never shows itself: ``repr``, ``str``, ``dump()`` and JSON responses give ``**********``; ``reveal()`` returns the text. [reference](https://hinoki.caspel.dev/dev/reference/schemas/#hinoki.fields.Secret)

- `hinoki.errors.ValidationError(details: 'list[ErrorDetail]') -> 'None'`: Input does not match a schema. [reference](https://hinoki.caspel.dev/dev/reference/schemas/#hinoki.errors.ValidationError)

- `hinoki.fields.UNSET`: Type of ``UNSET``: a field that was absent from the input (PATCH). [reference](https://hinoki.caspel.dev/dev/reference/schemas/#hinoki.fields.UNSET)

- `hinoki.derive.derive(...)`: Derive selected fields from ``model`` into a Schema subclass. [reference](https://hinoki.caspel.dev/dev/reference/schemas/#hinoki.derive.derive)
