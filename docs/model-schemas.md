---
status: implemented
related:
  - schemas.md
  - models.md
---

# Schemas derived from models

A `Schema` can take its fields and constraints from a `Model`. Put the source model on the class; use one `@derive(...)` decorator when you need to select or make fields optional. Without the decorator, every model column is derived.

```python
from hinoki import Schema, derive


@derive(only=("title", "slug", "body"))
class PostCreate(Schema, model=Post): ...


@derive(exclude=("author_id", "internal_note"))
class PostOut(Schema, model=Post):
    author_name: str


@derive(only=("title", "body"), partial=True)
class PostPatch(Schema, model=Post): ...


class PostAll(Schema, model=Post): ...
```

| Option | Effect |
| --- | --- |
| `model=Post` | source model; with no decorator, derives every column |
| `only=("a", "b")` | take exactly these columns |
| `exclude=("a",)` | take every column except these |
| `partial=True` | make every selected derived field optional (`UNSET` when absent) |
| `partial=("a",)` | make only the named selected fields optional |

`only` and `exclude` cannot be combined. An empty `only=()` is invalid; `exclude=()` is equivalent to selecting all columns. Model field types, maximum lengths, defaults and nullability are used. Annotations in the class body add fields or override a derived field, and methods and checks in the body are kept. Methods can use zero-argument `super()` as usual.

Unknown columns, a non-model, an empty `only`, or optional names outside the selection fail when the class is declared. Selection options require `model=` on the class. Relations are not derived as nested values; declare the API shape explicitly. Model derivation is explicit but includes all columns by default, so use `only` or `exclude` when the API must omit private fields.

## Add validation checks

Use `@field_check("name")` for a rule on one or more structurally valid fields, and `@object_check` for a rule over the whole object. Raise `Invalid(code, message, path=...)` to report a problem. Checks are synchronous and must not perform I/O; database-dependent rules belong in an operation.

```python
class SignUp(Schema):
    password: str
    confirmation: str

    @object_check
    def passwords_match(self) -> None:
        if self.password != self.confirmation:
            raise Invalid("mismatch", "passwords do not match", path="confirmation")
```

Structural errors are reported before custom checks. All applicable check failures are returned together. See [schemas](schemas.md) for the validation contract and conversion methods.
