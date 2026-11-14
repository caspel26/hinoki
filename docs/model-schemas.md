---
status: implemented
related:
  - schemas.md
  - models.md
---

# Schemas derived from models

A `Schema` can take its fields and constraints from a `Model`. You still choose which fields cross the API boundary. The choices are separate decorators, read top to bottom like [model options](model-options.md); there is no `class Meta` and no class keyword.

```python
from hinoki import Schema, derive


@derive.model(Post)
@derive.only("title", "slug", "body")
class PostCreate(Schema): ...


@derive.model(Post)
@derive.exclude("author_id", "internal_note")
class PostOut(Schema):
    author_name: str


@derive.model(Post)
@derive.only("title", "body")
@derive.partial()
class PostPatch(Schema): ...
```

| Decorator | Effect |
| --- | --- |
| `@derive.model(Post)` | the source model; put it **on top**, it reads the decorators below and builds the schema |
| `@derive.only("a", "b")` | take exactly these columns |
| `@derive.exclude("a")` | take every column except these (without `only` or `exclude`, every column is taken) |
| `@derive.partial()` | make every derived field optional (`UNSET` when absent), for partial updates; `@derive.partial("a")` only those |

`only` and `exclude` cannot be combined. Model field types, maximum lengths, defaults and nullability are used. Annotations in the class body add fields or override a derived field, and methods and checks in the body are kept. Methods can use zero-argument `super()` as usual.

Unknown columns, a non-model, or `only` with `exclude` fail when the class is declared. `only`, `exclude` or `partial` without `@derive.model(...)` above them fail on first use with a message naming the missing decorator. Relations are not derived as nested values; declare the API shape explicitly.

Earlier revisions took `model=`, `fields=`, `exclude=` and `optional=` as class keywords; they are removed, and `class PostOut(Schema, model=Post)` now raises `TypeError`.

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
