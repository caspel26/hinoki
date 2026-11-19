---
status: implemented
related:
  - model-schemas.md
  - errors.md
  - endpoints.md
---

# Schemas

A `Schema` describes data that crosses the API boundary and validates it strictly. Models describe tables; a column reaches a client only when a `Schema` names it.

```python
from typing import Annotated
from uuid import UUID

from hinoki import Field, Schema


class NoteIn(Schema):
    title: Annotated[str, Field(min_length=1, max_length=80)]
    body: str = ""

class NoteOut(Schema):
    id: UUID
    title: str
    body: str
```

* **Strict, no coercion.** `"1"` is not `1` (`Schema.validate_text()` and `Form[Schema]`, `XML[Schema]`, `Query[Schema]` are the explicit text-source exception, below); unknown keys are refused by default; every problem is reported with its path (capped at 50). Validation, instance construction and JSON serialisation run in Rust.
* **Partial updates.** Declare `title: str | Unset = UNSET`: a field the client did not send stays `UNSET` and is left out of `dump()` and `to_dict()`, so `row.aupdate_from(patch)` writes only what was sent.
* **Get data out.** `schema.to_dict()` gives Python values (UUID and datetime stay objects), `schema.dump()` the JSON-compatible form; both skip `UNSET`.
* **From rows.** `NoteOut.from_model(row, author_name=row.author.name)` converts one row; `NoteOut.from_models(rows)` eagerly converts a synchronous iterable to a list. Relations must already be loaded (nothing fetches lazily). A derived schema keeps its selected fields. Conversion reads the expected attributes rather than checking the concrete model class; missing attributes raise `AttributeError`. Values passed as `**extra` are shared across rows.
* **Into a row.** `await Note.acreate(data, author_id=me)` and `await row.aupdate_from(patch)`: keyword values set by the server win over the client's, and a schema can never carry the primary key.

`Schema.validate(data)` reports structural problems together. Values are not coerced; for data that is all text, such as form fields, `Schema.validate_text(data)` first converts values to the `int`, `float` and `bool` fields declare (and wraps a lone value of a `list` field), in Rust, then runs the same strict `validate`. A value that does not convert is left as text and fails validation with the usual type error; `str`, `UUID` and `datetime` are checked as text. Declare field constraints with `Field(...)`; use `@field_normalize` for narrow, typed string canonicalization and `@field_check`/`@object_check` for synchronous rejection checks.

## Field types

A field is annotated with `str`, `int`, `float`, `bool`, `UUID`, `datetime`, `date`, `time`, `timedelta`, `Decimal`, `bytes`, an `Enum` class, a `Literal`, a nested `Schema`, `list[X]`, `set[X]`, `frozenset[X]`, `tuple[X, ...]`, `tuple[X, Y]`, `dict[str, X]` or a [discriminated union](#discriminated-unions), optionally `| None` (accepts `null`) and `| Unset` (may be left out of a partial update), and optionally wrapped in `Annotated[..., Field(...)]`. `X` can be any of these, so `list[dict[str, int]]` and `dict[str, list[Point]]` work. Anything else (`bytearray`, `Any`, plain unions, `dict[int, X]`) raises `RegistrationError` when the class is created.

### Sets and tuples

`set[X]`, `frozenset[X]`, `tuple[X, ...]` and fixed tuples such as `tuple[float, float]` travel as JSON arrays; validation returns the Python container:

```python
from typing import Annotated

from hinoki import Field, Schema
from hinoki.errors import ValidationError


class Place(Schema):
    tags: Annotated[set[str], Field(max_length=5)]
    point: tuple[float, float]
    path: tuple[int, ...] = ()


place = Place.validate({"tags": ["a", "b", "a"], "point": [1.5, 2]})
assert place.tags == {"a", "b"} and place.point == (1.5, 2.0)
assert place.dump() == {"tags": ["a", "b"], "point": [1.5, 2.0], "path": []}

try:
    Place.validate({"tags": [], "point": [1.5]})
except ValidationError as error:
    assert [(d.path, d.code) for d in error.details] == [("point", "min_length")]
```

* **Duplicates** in a set collapse; `min_length` and `max_length` on a set or variadic tuple count the array as sent. A fixed tuple has its length from its type (`at least 2 items` / `at most 2 items`), each position is checked at `point[0]`, and `Field(...)` constraints are refused on it.
* **Hashable items.** Set and frozenset items must be hashable: scalars, enums, or tuples and frozensets of those. A set of `Schema`, `list`, `dict` or `set` items raises `RegistrationError`; bare `set`, `tuple[()]` and a misplaced `...` do too.
* **Construction** takes exactly the declared container (`Place(tags={"a"}, point=(1, 2))`); a list for a `set` field is refused.
* **Output.** `dump()`, responses and the JSON encoder write arrays, a set sorted when its items compare; `to_dict()` keeps the `set`, `frozenset` and `tuple`.
* **Models.** A `Model` column annotated with one of these is a JSON column holding an array; the model stores and reads lists, and only `Schema` fields build sets and tuples.
* **Lanes.** The native GIL-free CRUD lane does not read these fields; such a schema is validated through the Python lane, with the same results.
* **OpenAPI** documents a set as an array with `uniqueItems`, a variadic tuple as an array and a fixed tuple with `prefixItems`, `minItems` and `maxItems`.

### Enums and literals

An `Enum`, `StrEnum` or `IntEnum` class, or a `Literal` of strings or of integers, accepts only the listed values. On the wire the value is the member's value; validation returns the member itself:

```python
import enum
from typing import Literal

from hinoki import Schema
from hinoki.errors import ValidationError


class Status(enum.StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"


class Priority(enum.IntEnum):
    LOW = 1
    HIGH = 3


class Ticket(Schema):
    channel: Literal["web", "email"]
    status: Status = Status.DRAFT
    priority: Priority = Priority.LOW
    retries: Literal[0, 1, 3] = 0


ticket = Ticket.validate({"channel": "web", "status": "published", "priority": 3})
assert ticket.status is Status.PUBLISHED and ticket.priority is Priority.HIGH
assert ticket.dump() == {
    "channel": "web", "status": "published", "priority": 3, "retries": 0,
}

try:
    Ticket.validate({"channel": "sms", "priority": 2})
except ValidationError as error:
    assert [(d.path, d.code) for d in error.details] == [
        ("channel", "one_of"),
        ("priority", "one_of"),
    ]
```

* **Values, not names.** The names of the members never appear on the wire; the error message lists the accepted values (`must be one of 'web', 'email'`). Matching is strict: `"3"` and `3.0` are not `3`, and `True` is not `1`. Wrong types answer `type`, a value outside the list answers `one_of`.
* **One kind of value.** All values are `str`, or all are `int` (64-bit, never `bool`). A mixed `Enum` or `Literal`, `None` inside a `Literal` (write `Literal["a"] | None`), a `Flag` enum, an enum without members, an enum member inside a `Literal` (use the class) and values of any other type raise `RegistrationError`. Aliases of a member count once. `Field(...)` constraints, including `one_of`, are refused on these fields: the type is the list.
* **Construction** takes members for an enum field (`Ticket(status=Status.DRAFT)`) and the listed values for a `Literal`; a plain `"draft"` is refused for an enum field. `validate` and the request sources read the value, and a member of a `StrEnum` is read as its text.
* **Output.** `dump()`, responses and the JSON encoder write the member's value (inside `Schema` instances); `to_dict()` keeps the member.
* **Text sources.** Form, query and XML values are text; an `IntEnum` or an `int` `Literal` reads its number like an `int` field does.
* **Models.** A `Model` column annotated with an enum or a `Literal` stays a JSON column; only `Schema` fields check the values.
* **OpenAPI** documents the field as its `type` (`string` or `integer`) with an `enum` list of the values, inline in the property.

### Dates, durations, decimals and bytes

`date`, `time`, `timedelta`, `Decimal` and `bytes` travel as JSON strings and are validated identically by the Python and the Rust lane:

```python
import datetime as dt
from decimal import Decimal
from typing import Annotated

from hinoki import Field, Schema


class Booking(Schema):
    day: dt.date
    opens: dt.time
    length: dt.timedelta
    price: Annotated[Decimal, Field(ge=0, max_digits=8, decimal_places=2)]
    receipt: Annotated[bytes, Field(max_length=1024)]


booking = Booking.validate(
    {
        "day": "2026-03-14",
        "opens": "09:30:00+01:00",
        "length": "PT1H30M",
        "price": "19.90",
        "receipt": "aGVsbG8=",
    }
)
assert booking.length == dt.timedelta(hours=1, minutes=30)
assert booking.price == Decimal("19.90")
assert booking.receipt == b"hello"
assert booking.dump()["price"] == "19.90"
```

| Type | Accepted input | Output | Error code |
|---|---|---|---|
| `date` | `YYYY-MM-DD`, a real calendar date in years 0001–9999, ASCII digits | the same text | `date` |
| `time` | `HH:MM:SS[.f…f]` with 1 to 6 fraction digits and a `Z` or `±HH:MM` offset | `isoformat()` | `time`; `time_tz` when the offset is missing |
| `timedelta` | ISO 8601 `[-]P[nD][T[nH][nM][n[.f…f]S]]`; years, months and weeks are refused | `PT1H30M`, `P1DT2S`, `-PT0.5S` | `duration`; `duration_range` outside `timedelta`'s range |
| `Decimal` | a string `-?(0\|[1-9][0-9]*)(\.[0-9]+)?` or an integer; floats, booleans, exponents, `NaN` and infinities are refused | a fixed-point string that keeps the scale (`"0.10"`) | `decimal` |
| `bytes` | standard padded base64 (`+`, `/`, `=`); no whitespace or URL-safe alphabet | padded base64 | `bytes` |

* **A time needs an offset** on the wire, as a `datetime` does; a time without one is reported as `time_tz`. Fields derived from a `Model` through `from_model()` accept naive times, as they accept naive datetimes. A `date` field refuses a `datetime`.
* **`Decimal` is exact.** Bounds `ge`, `le`, `gt` and `lt` (an `int` or `Decimal`, never a float) compare exactly. `max_digits` (default 64, at most 1000) counts all digits, `decimal_places` the digits after the point, and when `decimal_places` is declared the digits before the point are limited to `max_digits - decimal_places`. The codes are `max_digits`, `decimal_places` and `max_whole_digits`; the checks run in that order, then the bounds. A `decimal_places` above `max_digits` is refused at class creation.
* **`bytes` size.** `min_length` and `max_length` count decoded bytes (`at most 1024 bytes`). Client input without a `max_length` is limited to 65536 decoded bytes (`hinoki.fieldtypes.MAX_BYTES_LENGTH`); a declared `max_length` replaces the limit. Fields derived from a `Model` column never get it. `pattern` and `format=` do not apply to `bytes`.
* **Text sources.** Form, query and XML values use the same text forms, so `validate_text` needs no conversion for these types.
* **Models.** A `bytes` column stores as `BYTEA` on PostgreSQL and a blob on SQLite, and native CRUD serialises it as base64. Constraints on a `bytes` model column are refused. The ORM stores only `bytes` among these types; `date`, `time`, `timedelta` and `Decimal` are not model column types yet.
* **OpenAPI** documents `date` as `format: date`, `time` as `format: time`, `timedelta` as `format: duration`, `Decimal` as a `string` with `format: decimal` and a `pattern`, and `bytes` as `format: byte` with `contentEncoding: base64`. Decimal bounds appear as `minimum`/`maximum` numbers.
* **Output.** The JSON encoder writes these values only inside `Schema` instances; a bare `Decimal` or `bytes` in a plain dictionary is still a `TypeError`, so a handler that returns one must return a `Schema` or supply a `default=` hook. A `Decimal` that is `NaN`, infinite or has more than 1000 digits raises `ValueError`.

### Dictionary fields

Use `dict[str, X]` for an object whose keys the client chooses, such as labels, per-locale texts or settings by name:

```python
from typing import Annotated

from hinoki import Field, Schema


class Point(Schema):
    x: int
    y: int = 0


Locale = Annotated[str, Field(min_length=2, max_length=5, pattern=r"[a-z]{2}(-[A-Z]{2})?")]


class Place(Schema):
    labels: Annotated[dict[str, Annotated[str, Field(max_length=40)]], Field(max_length=20)]
    names: dict[Locale, Annotated[str, Field(max_length=80)]] = Field(default_factory=dict)
    points: dict[str, Point] = Field(default_factory=dict)


place = Place.validate({"labels": {"kind": "park"}, "points": {"gate": {"x": 1}}})
assert place.points == {"gate": Point(x=1)}
assert place.dump() == {
    "labels": {"kind": "park"},
    "names": {},
    "points": {"gate": {"x": 1, "y": 0}},
}
```

* **Keys are strings.** `dict[int, X]`, `dict[UUID, X]` and a bare `dict` are refused at class creation with a message that names `dict[str, X]`. Every key is an ordinary string: `"__proto__"` or `"constructor"` has no special meaning.
* **Values** follow the value type, with its own `Field(...)` constraints, nested `Schema`s, lists, unions and `None` when the value type allows it. A whole `dict[...] | None` field accepts `null`.
* **Error paths** add the key as one more dotted segment, as list items add `[index]`: `labels.kind`, `points.gate.x`, `scores.math[1]`. A key longer than 64 characters is shortened to its first 64 in the path, as unknown field names are. The codes and messages are those of the value type.
* **Entry count.** `Field(min_length=..., max_length=...)` on the dict counts entries, exactly like a list: `at least 1 item`, `at most 20 items`. A `min_length` above the maximum in force is refused at class creation.
* **Key constraints.** Annotate the key type to constrain keys: `dict[Annotated[str, Field(...)], X]` accepts `min_length`, `max_length`, `pattern` and `format=`. A key that breaks one of these is reported once at its path with `key_min_length`, `key_max_length` or `key_pattern` (for example `key must have at most 5 characters`), and its value is not checked. A key that is not valid Unicode text (a lone surrogate in a Python string) is reported as `type` at the field path.
* **Client input is always bounded.** On wire input (`Schema.validate(...)` and request bodies), a dict without `max_length` accepts at most 1000 entries (`hinoki.fieldtypes.MAX_MAPPING_ITEMS`) and a key without its own `max_length` at most 256 characters (`hinoki.fieldtypes.MAX_MAPPING_KEY_LENGTH`). A declared `max_length` replaces these bounds. They do not apply to values the server builds: direct construction, `from_model()` and response projections accept larger stored dicts, and fields derived from a model column never get them. Declared limits apply everywhere.
* **Order and duplicates.** Entries keep the order the client sent through `validate()`, `dump()`, `to_dict()` and the JSON response. A key repeated in a JSON body behaves as in any JSON object: the last value wins and the key keeps its first position.
* **Construction.** `Place(points={"gate": Point(x=1)})` validates the dict like any field; a nested value must already be a `Schema` instance and a non-string key is reported as `object keys must be strings`. `@field_check` and `@object_check` of nested values run with the same key paths. `from_model()` copies a row's dict, as it copies lists.
* **OpenAPI** describes the field as `type: object` with `additionalProperties` set to the value schema, `propertyNames` set to the key schema and `minProperties`/`maxProperties` from declared entry limits. The wire-input bounds above are not written into the document, because the same component also describes responses, which they do not limit.

A text value without a limit is reported by the `HNK-SCH-002` rule like a text field (see [rules](rules.md)). `HNK-SCH-001` (`Schemas.forbid_exposure`) checks field names only: the keys of a dict are runtime data, so a forbidden name used as a key is not detected. A `Model` column that holds a dict, directly or in list items, stays a JSON column: model writes do not apply these checks, and constraints on such a column are refused. A `Schema` derived from that model gets a `dict[str, X]` field validated as described here, without the wire-input bounds.

Checks of `Schema` values run through lists and dicts at any depth. A `list[list[Point]]` field runs `Point`'s `@field_check` and `@object_check` for each inner item (`rows[1][0].x`); earlier versions skipped the checks of lists nested in lists.

## Wire aliases

Use `Field(alias=...)` when the public JSON name should differ from the Python attribute. The alias is the only accepted input key; sending both names is an error. `dump()` writes aliases, including in nested schemas and lists, while `to_dict()` keeps Python attribute names. Validation errors and OpenAPI properties use the wire names. A custom check's default field path is translated to its alias; an explicit `Invalid(path=...)` is already a wire path.

```python
from typing import Annotated

from hinoki import Field, Schema


class PersonOut(Schema):
    first_name: Annotated[str, Field(alias="firstName")]


person = PersonOut.validate({"firstName": "Aki"})
assert person.first_name == "Aki"
assert person.dump() == {"firstName": "Aki"}
assert person.to_dict() == {"first_name": "Aki"}
```

An assigned declaration such as `display_name: str = Field(alias="displayName")` is also supported, including alongside `default_factory`. Aliases must be unique within a schema and cannot contain `.`, `[` or `]`, which are reserved by error paths. Model columns do not accept wire aliases; put API naming on the `Schema`.

## Class configuration

`SchemaConfig` is an immutable typed configuration passed in the class declaration. By default, unknown string keys in decoded input are rejected. `SchemaExtra.IGNORE` discards them while keeping declared fields strict:

```python
from hinoki import Schema, SchemaConfig, SchemaExtra


class VendorEvent(Schema, config=SchemaConfig(extra=SchemaExtra.IGNORE)):
    event_id: str
    revision: int


event = VendorEvent.validate(
    {"event_id": "evt-1", "revision": 2, "vendor_context": {"trace": "abc"}}
)
assert event.dump() == {"event_id": "evt-1", "revision": 2}
```

Ignored values are not stored as attributes or emitted by `dump()`. A wrong type, failed constraint or missing required field among the declared fields still raises `ValidationError` before either a sync or async handler runs. This option applies to decoded `Schema.validate(...)` input and request bodies. Generated constructor keywords and explicit `from_model(..., **extra)` overrides keep their normal strict signatures. Each nested `Schema` uses its own configuration.

An alias remains the only accepted wire name. If `given_name` has alias `givenName`, sending only `given_name` is ignored as an extra and the required `givenName` field remains missing; sending both names is still a duplicate-field error. OpenAPI marks an IGNORE schema with `additionalProperties: true`, while its response serialization still includes only declared fields.

`Schema(...)` validates the final Python values immediately and raises `ValidationError` for invalid values. Pass Python objects such as `UUID`, timezone-aware `datetime` and nested `Schema` instances. `Schema.validate(decoded_data)` is the wire-input path: it accepts JSON-compatible values, resolves aliases and returns field errors using wire names. Request body binding uses that path before calling the handler. `from_model()` also validates the constructed Schema; naive datetimes returned by database drivers remain unchanged and are allowed on that model-conversion path. When a handler declares one concrete Schema return type, a direct Model result is passed through `from_model()` automatically (an invalid row is a `500`, not a `422`) and a subclass instance is sent with the declared fields only; this does not validate other handler return values such as a `dict`. See [Schemas derived from models](model-schemas.md#return-a-model-from-a-schema-annotated-handler).

## Discriminated unions

Use a discriminated union when one field can contain one of several known Schema shapes. Declare the union on a `Schema` field with `Annotated[Cat | Dog, Field(discriminator="kind")]`. Every variant needs the same required string field, the same wire alias for that field, and exactly one `Field(one_of=(...))` tag. Hinoki selects the variant by that tag in Rust; it does not try each schema in order.

```python
from typing import Annotated

from hinoki import Field, Schema, http


class Cat(Schema):
    kind: Annotated[str, Field(alias="type", one_of=("cat",))]
    name: str


class Dog(Schema):
    kind: Annotated[str, Field(alias="type", one_of=("dog",))]
    walks: int


Pet = Annotated[Cat | Dog, Field(discriminator="kind")]


class PetRequest(Schema):
    pet: Pet


@http.post("/pets")
async def create_pet(data: PetRequest) -> PetRequest:
    return data
```

`PetRequest.validate({"pet": {"type": "cat", "name": "Mugi"}})` returns a `PetRequest` whose `pet` is a `Cat`; manual construction takes an already validated variant instance, such as `PetRequest(pet=Dog(kind="dog", walks=2))`. A dict is not coerced into a variant. `list[Pet]`, nested Schema fields, nullable unions and `Unset` use the same discriminator rules. Unknown tags produce a field error at the tag's wire path without echoing the submitted value. The handler is not called for an invalid body, whether it is synchronous or asynchronous.

Each variant's tag field is required and cannot have a default. Tags must be unique strings, variants must be distinct Schema classes, and variant classes cannot override `dump()` because the override could remove or change the tag on the wire. Plain unions without `Field(discriminator=...)`, non-Schema variants, and unions directly in handler body or return annotations are not supported; wrap the union in a Schema for route input and output. The runtime check of return values is separate and applies to every action.

## Projection options on `dump()`

Use `dump()` when application code needs a JSON-compatible projection of a Schema. `include` and `exclude` accept Python field names and apply to the top-level fields only; exclusion wins if a field appears in both. There are no dotted paths or nested selection maps. Nested Schemas and supported lists are converted recursively, with `by_alias` applied at every Schema level.

```python
from typing import Annotated

from hinoki import Field, Schema


class PersonOut(Schema):
    first_name: Annotated[str, Field(alias="firstName")]
    bio: str = ""


person = PersonOut.validate({"firstName": "Aki"})
assert person.dump() == {"firstName": "Aki", "bio": ""}
assert person.dump(exclude_unset=True) == {"firstName": "Aki"}
assert person.dump(include={"first_name"}, by_alias=False) == {
    "first_name": "Aki"
}
assert person.dump(include=set()) == {}
```

`exclude_unset=True` omits values supplied by dataclass defaults or default factories when the caller did not provide that field. It does not compare values with defaults: explicitly passing `bio=""`, `None`, `0`, `False` or an empty collection keeps that value. `UNSET` is omitted regardless of this option. `from_model()` tracks attributes it actually reads and values passed via `**extra`; missing optional attributes that use Schema defaults remain unset. A custom `__post_init__` changing a field does not make it supplied. A custom `__init__` has no reliable input-presence information, so `exclude_unset=True` raises `TypeError` for its instances.

With no options, `dump()` preserves its current output and honors custom `dump()` overrides, including nested ones. Root-only `include` and `exclude` can select a custom-dumped nested value; recursive controls (`exclude_unset=True` or `by_alias=False`) refuse that value because Hinoki cannot safely inspect the override. A zero-argument override continues to work at runtime for automatic HTTP output, but widening the base signature means mypy flags that override; typed overrides that use the new options must accept the same keyword-only signature. Filtering a manually returned dictionary does not change the OpenAPI contract and is not an authorization mechanism; declare a response Schema containing only fields the caller may see. `to_dict()` remains the Python-value conversion and its existing `exclude=` behavior is unchanged.

`dump` is reserved as a `Schema` method name. Declaring a field with that Python attribute name raises `RegistrationError` instead of treating the inherited method as a default value; rename the Python field to avoid the collision. A wire alias alone cannot resolve a Python attribute-name conflict.

## Built-in and custom validation

`Field(...)` provides built-in structural constraints. `format=` accepts `email`, `url`, `slug` or `hex_color` and is shorthand for a regular-expression pattern. Numeric bounds apply to numbers; length limits apply to strings and collections; `one_of` restricts a value to the listed choices.

```python
from typing import Annotated

from hinoki import (
    Field,
    Invalid,
    Schema,
    field_check,
    field_normalize,
    object_check,
)


class Registration(Schema):
    username: Annotated[str, Field(min_length=3, max_length=32, pattern=r"[a-z0-9_]+")]
    email: Annotated[str, Field(format="email")]
    password: Annotated[str, Field(min_length=12)]
    confirmation: str

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
                "password_mismatch",
                "passwords do not match",
                path="confirmation",
            )
```

Structural validation runs first and rejects wrong types, missing required values, unknown keys and failed constraints. `@field_normalize("email")` then receives a valid string and returns its canonical form; the result is checked against the same `Field(...)` constraints before it is applied. The same hook runs for `Schema.validate(...)`, direct `Schema(...)` construction and `from_model(...)`. It cannot rescue an input that failed the initial validation. Normalizers must be typed synchronous methods returning `str`; they cannot perform I/O or reject input.

After normalization, field checks receive final values and object checks can compare final fields. Raise `Invalid(code, message)` to reject a value; these checks return `None` and do not transform it. Non-`None` returns and async check methods are programming errors. Applicable custom-check failures are returned together in `errors.ValidationError.details`. Checks must be side-effect free; do not query the database or call external services. Put checks that depend on current data in an operation.

These decorators validate a `Schema` instance passed through `Schema.validate`; they are not run on a `Model`. Model fields have separate checks that run before ORM writes, described in [Model field constraints and validators](models.md#model-field-constraints-and-validators).

See also the [glossary](glossary.md) for what "schema" does and does not mean in these docs.
