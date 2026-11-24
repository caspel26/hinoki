# Collections and unions

A field can hold `list[X]`, `set[X]`, `frozenset[X]`, `tuple[X, ...]`,
`tuple[X, Y]`, `dict[str, X]`, a nested `Schema`, a `TypedDict`, a dataclass,
a plain union, a discriminated union or `Any`. `X` is any supported type, so
`list[dict[str, int]]` works. Error paths add `[index]` for items and `.key`
for entries: `rows[1][0].x`, `points.gate.x`.

## Sets and tuples

```python
from typing import Annotated

from hinoki import Field, Schema


class Place(Schema):
    tags: Annotated[set[str], Field(max_length=5)]
    point: tuple[float, float]
    path: tuple[int, ...] = ()


place = Place.validate({"tags": ["a", "b", "a"], "point": [1.5, 2]})
assert place.tags == {"a", "b"} and place.point == (1.5, 2.0)
assert place.dump() == {"tags": ["a", "b"], "point": [1.5, 2.0], "path": []}
```

* They travel as JSON arrays. Set duplicates collapse; length limits count the
  array as sent. A fixed tuple's length comes from its type and refuses
  `Field(...)`.
* Set items must be hashable (scalars, enums, tuples and frozensets of those).
  Bare `set`, `tuple[()]` and a misplaced `...` raise `RegistrationError`.
* Construction takes exactly the declared container; `to_dict()` keeps it,
  `dump()` writes arrays (sets sorted when comparable).
* No model column type: store a `list[X]` (JSON) and build sets in the
  `Schema`. Served by the Python lane.
* **OpenAPI:** `uniqueItems` for sets; `prefixItems`, `minItems`, `maxItems`
  for fixed tuples.

## Dictionary fields

```python
from typing import Annotated

from hinoki import Field, Schema

Locale = Annotated[
    str, Field(min_length=2, max_length=5, pattern=r"[a-z]{2}(-[A-Z]{2})?")
]
Label = Annotated[str, Field(max_length=40)]
Name = Annotated[str, Field(max_length=80)]


class Point(Schema):
    x: int
    y: int = 0


class Venue(Schema):
    labels: Annotated[dict[str, Label], Field(max_length=20)]
    names: dict[Locale, Name] = Field(default_factory=dict)
    points: dict[str, Point] = Field(default_factory=dict)


venue = Venue.validate({"labels": {"kind": "park"}, "points": {"gate": {"x": 1}}})
assert venue.points == {"gate": Point(x=1)}
```

* **Keys are strings.** `dict[int, X]`, `dict[UUID, X]` and bare `dict` are
  refused at class creation. Constrain keys with
  `dict[Annotated[str, Field(...)], X]` (`min_length`, `max_length`,
  `pattern`, `format`); a bad key answers `key_min_length`, `key_max_length` or
  `key_pattern` and its value is not checked.
* **Entry count.** `Field(min_length=, max_length=)` on the dict counts entries.
* **Client input is bounded.** Without `max_length`, wire input accepts at most
  1000 entries (`MAX_MAPPING_ITEMS`) and keys of at most 256 characters
  (`MAX_MAPPING_KEY_LENGTH`); declared limits replace these. Values the server
  builds (construction, `from_model()`) are not bounded.
* **Order** is kept as sent; a repeated JSON key keeps its first position and
  last value. Paths shorten keys longer than 64 characters.
* **Models.** A dict column is a JSON column: model writes do not apply these
  checks. A `Schema` derived from it validates as above without wire bounds.
* `HNK-SCH-002` checks text values for limits; `HNK-SCH-001` checks field
  names only, not keys.
* **OpenAPI:** `type: object` with `additionalProperties`, `propertyNames` and
  declared `minProperties`/`maxProperties`.

## Plain unions and `Any`

```python
from typing import Annotated, Any

from hinoki import Field, Schema


class Reading(Schema):
    value: int | str
    when: Annotated[int, Field(ge=0)] | Annotated[str, Field(max_length=10)]
    extra: Any = None


reading = Reading.validate({"value": "abc", "when": 5, "extra": {"k": [1, None]}})
assert reading.value == "abc" and reading.when == 5
```

* **First match wins**, in declared order; `bool` is never an `int`. No match
  answers `union` once at the field path.
* 2 to 16 variants; constraints go on a variant, not the union. A union of
  only `Schema` classes needs a discriminator (below).
* `Any` and `object` accept every JSON value unchanged; bound their size
  elsewhere.
* Text sources take the first `int`, `float` or `bool` variant that parses,
  else the `str` variant.
* More than 1024 failed container trials inside one union refuse the request.
* Checked in Rust on the Python-object path; not lowered to the GIL-free JSON
  lane. **OpenAPI:** `anyOf`. Model columns with a union or `Any` are refused by
  migrations.

## Discriminated unions

```python
from typing import Annotated

from hinoki import Field, Schema


class Card(Schema):
    kind: Annotated[str, Field(alias="type", one_of=("card",))]
    last4: Annotated[str, Field(min_length=4, max_length=4)]


class Transfer(Schema):
    kind: Annotated[str, Field(alias="type", one_of=("transfer",))]
    iban: Annotated[str, Field(max_length=34)]


Method = Annotated[Card | Transfer, Field(discriminator="kind")]


class PaymentIn(Schema):
    method: Method
```

`PaymentIn.validate({"method": {"type": "card", "last4": "4242"}})` returns a
`Card`, selected by its tag in Rust without trying each variant.

* Every variant has the same required string tag field (no default), the same
  alias, and exactly one `Field(one_of=(...))` value; tags are unique and
  variants distinct.
* An unknown tag is a field error at the tag's wire path, without echoing the
  value.
* Construction takes a variant instance, never a dict. Variants cannot override
  `dump()`.
* `list[Method]`, nested fields, `| None` and `| Unset` follow the same rules.
  A union directly in a handler's body or return annotation is not supported:
  wrap it in a `Schema`.

## `TypedDict` and dataclass fields

```python
from dataclasses import dataclass
from typing import NotRequired, TypedDict

from hinoki import Schema


class Address(TypedDict):
    street: str
    zip: NotRequired[str]


@dataclass
class Geo:
    lat: float
    lon: float = 0.0


class Site(Schema):
    address: Address
    geo: Geo | None = None
```

* Hinoki generates one `Schema` per type with the same name and fields; the
  value is an instance of it (`site.address.street`), not a `dict` or `Geo`.
  Methods, `__post_init__` and bases are not carried over.
* A key that may be missing becomes `| Unset`; a dataclass field with a default
  is optional.
* Member `Field(...)` constraints are honoured. `init=False` and recursive types
  raise `RegistrationError`.
* **OpenAPI:** one component named after the type.

## See also

- [Scalar field types](https://hinoki.caspel.dev/dev/schema-types.md)
- [Validate input](https://hinoki.caspel.dev/dev/schema-validation.md)
- [OpenAPI](https://hinoki.caspel.dev/dev/openapi.md)
