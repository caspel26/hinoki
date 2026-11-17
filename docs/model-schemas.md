---
status: implemented
related:
  - schemas.md
  - models.md
---

# Schemas derived from models

```python
from blog.models import Post
from hinoki import Schema, derive
```

The validation example below is independent and includes its own imports.

A `Schema` can take its fields and constraints from a `Model`. Pass the source model to the single `@derive(Model, ...)` decorator when selecting or making fields optional. A class without a decorator can still use `model=Model` to derive every column during the compatibility cycle.

```python
from hinoki import Schema, derive


@derive(Post, only=("title", "slug", "body"))
class PostCreate(Schema): ...


@derive(Post, exclude=("author_id", "internal_note"))
class PostOut(Schema):
    author_name: str


@derive(Post, only=("title", "body"), partial=True)
class PostPatch(Schema): ...


@derive(Post)
class PostAll(Schema): ...
```

| Option | Effect |
| --- | --- |
| `@derive(Post, ...)` | source model and field selection |
| `only=("a", "b")` | take exactly these columns |
| `exclude=("a",)` | take every column except these |
| `partial=True` | make every selected derived field optional (`UNSET` when absent) |
| `partial=("a",)` | make only the named selected fields optional |

`only` and `exclude` cannot be combined. An empty `only=()` is invalid; `exclude=()` is equivalent to selecting all columns. Model field types, maximum lengths, defaults and nullability are used. Annotations in the class body add fields or override a derived field, and methods and checks in the body are kept. Methods can use zero-argument `super()` as usual.

Unknown columns, a non-model, an empty `only`, or optional names outside the selection fail when the class is declared. Relations are not derived as nested values; declare the API shape explicitly. Model derivation includes all columns by default, so use `only` or `exclude` when the API must omit private fields. During the compatibility cycle, `@derive(options...)` with `model=Model` remains valid and emits `DeprecationWarning`; use `@derive(Model, options...)` for new code.

## Return a Model from a Schema-annotated handler

When a handler declares one concrete response `Schema`, Hinoki converts a
directly returned `Model` with that Schema's `from_model()` projection:

```python
from uuid import UUID

from blog.models import Post
from hinoki import Schema, derive, http


@derive(Post, only=("id", "title"))
class PostSummary(Schema): ...


@http.get("/posts/{post_id}")
async def one(post_id: UUID) -> PostSummary:
    return await Post.aget(id=post_id)
```

An existing `PostOut` return value still works. The conversion applies to one
Model result only; lists, pages, unions and `Response` values keep their existing
explicit handling. No relation is loaded implicitly, and the Schema return
annotation does not validate handler output. For a computed field such as
`author_name`, keep using `PostOut.from_model(post, author_name=...)`.

Static type checkers still see a Model return as incompatible with a Schema
annotation; mark that intentional return with `# type: ignore[return-value]`.
Derived fields are also not synthesized for static type checkers, so declare
fields in the Schema body when they need static checking.

## Add validation checks

Use `@field_check("name")` for a rule on one or more structurally valid fields, and `@object_check` for a rule over the whole object. Raise `Invalid(code, message, path=...)` to report a problem. Checks are synchronous and must not perform I/O; database-dependent rules belong in an operation.

```python
from typing import Annotated

from hinoki import Field, Invalid, Schema, field_check, object_check


class SignUp(Schema):
    username: Annotated[str, Field(min_length=3, max_length=32)]
    password: Annotated[str, Field(min_length=12)]
    confirmation: str

    @field_check("username")
    def username_characters(self, value: str) -> None:
        if not value.replace("_", "").isalnum():
            raise Invalid("username_characters", "use letters, numbers and _ only")

    @field_check("password")
    def reject_common_password(self, value: str) -> None:
        if value.lower() in {"password12345", "qwerty123456"}:
            raise Invalid("common_password", "choose a less common password")

    @object_check
    def passwords_match(self) -> None:
        if self.password != self.confirmation:
            raise Invalid("mismatch", "passwords do not match", path="confirmation")
```

Structural errors are reported before custom checks. All applicable check failures are returned together. See [schemas](schemas.md) for the validation contract and conversion methods.
