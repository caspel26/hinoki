---
status: implemented
related:
  - schemas.md
  - models.md
  - errors.md
---

# Validate input

A `Schema` validates in three stages: Rust checks types and `Field(...)`
constraints, then `@field_normalize` canonicalizes values, then
`@field_check` and `@object_check` reject what the rules forbid. All failures
are returned together in `errors.ValidationError.details`, and a request
answers 422.

## Built-in and custom validation

```python
# notes/apps/accounts/schemas.py
from typing import Annotated

from hinoki import Field, Invalid, Schema, field_check, field_normalize, object_check

Username = Annotated[str, Field(min_length=3, max_length=32, pattern=r"[a-z0-9_]+")]


class Registration(Schema):
    username: Username
    email: Annotated[str, Field(format="email", max_length=254)]
    password: Annotated[str, Field(min_length=12, max_length=128)]
    confirmation: Annotated[str, Field(max_length=128)]

    @field_normalize("email")
    def lowercase_email(self, value: str) -> str:
        return value.lower()

    @field_check("password")
    def reject_common_password(self, value: str) -> None:
        if value.lower() in {"password12345", "qwerty123456"}:
            raise Invalid("common_password", "choose a less common password")

    @object_check
    def passwords_match(self) -> None:
        if self.password != self.confirmation:
            raise Invalid(
                "password_mismatch", "passwords do not match", path="confirmation"
            )
```

| Tool | Runs | Use for |
| --- | --- | --- |
| `Field(...)` | first, in Rust | lengths, `pattern`, numeric bounds, `one_of`, `format` (`email`, `url`, `slug`, `hex_color`) |
| `Field(strip=, lower=, upper=)` | before every other check | trimming and case folding of `str` |
| `@field_normalize("f")` | after structural validation | typed canonical form; the result is checked against the same constraints |
| `@field_check("f")` | after normalization | rejecting one value |
| `@object_check` | after field checks | rules across fields |

* Checks raise `Invalid(code, message, path=...)` and return `None`; a
  non-`None` return or an `async` method is a programming error.
* Normalizers are typed synchronous methods returning `str`; they cannot
  reject input or rescue a value that failed structural validation.
* All three run for `Schema.validate(...)`, `Schema(...)` and `from_model()`.
  They never run on a `Model`: model writes have their own
  [constraints and validators](models.md#model-field-constraints-and-validators).
* No I/O and no side effects. Rules that depend on current data belong in an
  operation.

## Normalize text

```python
class Signup(Schema):
    handle: Annotated[str, Field(strip=True, lower=True, min_length=3, max_length=32)]


Signup.validate({"handle": "  AbC "}).handle  # "abc"
```

`strip` removes what `str.strip()` removes; `lower` and `upper` follow
`str.lower()`/`str.upper()`. Constraints see the normalized text, and a custom
normalizer receives it. Only `str` fields accept these options; `lower` and
`upper` together, or a `one_of` value the normalization would change, raise
`RegistrationError`. Results are the same on both lanes and for direct
construction. The OpenAPI document describes the constraints, not the
normalization.

## Reshape input with `@before_validate`

```python
from typing import Any

from hinoki import before_validate


class Profile(Schema):
    display_name: Annotated[str, Field(max_length=80)]

    @before_validate
    @classmethod
    def accept_legacy_name(cls, data: dict[str, Any]) -> dict[str, Any]:
        if "name" in data and "display_name" not in data:
            rest = {key: value for key, value in data.items() if key != "name"}
            return {**rest, "display_name": data["name"]}
        return data


assert Profile.validate({"name": "Ada"}).display_name == "Ada"
```

* Runs on the decoded mapping before structural validation, on this schema and
  every nested one, on both lanes; base-class hooks first.
* The data is untrusted: check types first. It is a shallow copy; replace
  nested values instead of mutating them. Synchronous, no I/O.
* `Invalid(...)` answers 422; `KeyError`, `TypeError`, `ValueError`,
  `IndexError` or `AttributeError` answer `invalid_input`, not 500.
* A plain union of schemas with such a hook is refused: use a
  [discriminated union](schema-collections.md#discriminated-unions). On a
  plain method the decorator raises `RegistrationError`.

## Which lane runs what

Types and `Field(...)` constraints are checked in Rust on the Python-object
path and, for most schemas, on the GIL-free JSON path. The decorators are
Python callables: they run only on values that passed the Rust stage. Checks of
nested `Schema` values run through lists and dicts at any depth.

## One-pass JSON body

On Kiso, a JSON request body for a `Schema` parameter is read straight into
the instance's fields in Rust, without building the intermediate dict and list
tree. It applies to plain schemas: strings, integers, floats, booleans, lists
and nested `Schema` fields with length and numeric bounds, without `pattern`,
`normalize`, `one_of`, collections, `@before_validate` or checks. Anything else
runs the lane above on the same bytes, so the result and the error messages
(`path`, `code`, `message`) are the same either way: the one-pass reader
declines every invalid body and every unusual input (duplicate or unknown keys,
escapes in keys, NUL, lone surrogates, integers beyond 64 bits, nesting of 100
levels or more) and never reports an error itself.

`Schema.validate_json(raw)` exposes it: it returns the instance, or `None`
when the ordinary `core.decode_json` and `Schema.validate` must run.

```python
from hinoki import Schema


class Ticket(Schema):
    title: str
    seats: int


Ticket.validate_json(b'{"title": "Opera", "seats": 2}')  # a Ticket
Ticket.validate_json(b'{"title": "Opera"}')  # None: validate() reports it
```

### Native constructor and `validate`

For the same plain schemas, `Schema(...)`, `Schema.validate(data)`,
`Schema.validate_text(data)`, `instance.replace(...)` and `instance.dump()` run
in Rust: the arguments are checked and written to the slots without a Python
frame, defaults and `default_factory` values included. The result, the
exceptions and `dump(exclude_unset=True)` are the same as on the Python path.
Anything the plan does not vouch for (positional arguments, a value of a
subclass type, an unknown or missing argument, a class that overrides
`_validate_instance`, a `validate` of its own, or a graph with checks,
normalizers or `@before_validate`) runs the ordinary Python code. A
`default_factory` whose result is rejected may be called a second time by that
fallback. `Schema.validate(data)` reads a plain `dict` (and nested dicts and
lists of plain schemas) straight into the slots; a dict it does not fully
recognise, for instance with an unknown key, goes through the general validator.
`copy.copy`, `copy.deepcopy` and pickling of an instance also run in Rust over
the slots; a class that defines `__copy__`, `__deepcopy__`, `__reduce__`,
`__reduce_ex__` or `__getstate__` itself keeps its own.

### Where fields live

A `Schema` subclass keeps its fields in `__slots__` (the instance still has a
`__dict__` for attributes you set yourself), which lets Rust fill them without a
lookup per field. Consequences: field values are not in `vars(instance)`. A class can
combine only one base that declares fields with slots; declare the others with
`slots=False`:

```python
class Audited(Schema, slots=False):
    created_by: str


class Ticket(Audited):
    title: str
```

Without it, two field-bearing bases raise a `RegistrationError` that names
`slots=False`.

`Schema` has its own metaclass, so a schema cannot also inherit from a class
whose metaclass is `abc.ABCMeta` (`abc.ABC`, a `Protocol`): Python reports a
metaclass conflict. Keep such a base out of the schema's bases.

## Text that cannot be stored

A string or dict key containing NUL (`\u0000`) is refused with `nul` or
`key_nul` on both lanes, after the length checks and before `pattern`:
PostgreSQL cannot store NUL in `text`. A string that is not valid Unicode (a
lone surrogate) is refused with `type`.

## See also

- [Schemas](schemas.md)
- [Errors](errors.md): the 422 response body
- [Define models](models.md#model-field-constraints-and-validators)
