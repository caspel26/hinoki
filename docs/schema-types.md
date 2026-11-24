---
status: implemented
related:
  - schemas.md
  - schema-collections.md
  - openapi.md
---

# Scalar field types

A `Schema` field can be `str`, `int`, `float`, `bool`, `UUID`, `datetime`,
`date`, `time`, `timedelta`, `Decimal`, `bytes`, an `Enum` class, a `Literal`,
an `ipaddress` type, `Email`, `Url`, `Slug`, `Secret` or `Path`; optionally
`| None` (accepts `null`) or `| Unset`, and wrapped in
`Annotated[..., Field(...)]`. Containers, unions and nested types are in
[collections and unions](schema-collections.md). Anything else raises
`RegistrationError` when the class is created.

## Numbers

JSON input is strict: a float is never an integer, and `bool` is never a
number. A `float` field always holds a `float`: JSON `1` becomes `1.0`, and
`Reading(value=1).value` is `1.0`, also inside lists, sets, tuples and
`dict[str, float]`. A `float` variant of a plain union keeps the value it was
given.

### Multiples

`Field(multiple_of=step)` requires an `int` or `Decimal` to be an exact whole
multiple of a positive step. It runs after the bounds and fails with
`multiple_of`. A `float` field refuses `multiple_of` (float multiples are
inexact): use `Decimal`.

```python
from decimal import Decimal
from typing import Annotated

from hinoki import Field, Schema


class Order(Schema):
    quantity: Annotated[int, Field(ge=0, multiple_of=5)]
    price: Annotated[Decimal, Field(multiple_of=Decimal("0.05"), max_digits=8)]


Order.validate({"quantity": 10, "price": "9.95"})  # accepted
Order.validate({"quantity": 12, "price": "9.97"})  # two multiple_of errors
```

## Enums and literals

An `Enum`, `StrEnum` or `IntEnum` class, or a `Literal` of strings or of
integers, accepts only its values. The wire carries the value; validation
returns the member.

```python
import enum
from typing import Literal

from hinoki import Schema


class Status(enum.StrEnum):
    DRAFT = "draft"
    SENT = "sent"


class Priority(enum.IntEnum):
    LOW = 1
    HIGH = 3


class Ticket(Schema):
    channel: Literal["web", "email"]
    status: Status = Status.DRAFT
    priority: Priority = Priority.LOW


ticket = Ticket.validate({"channel": "web", "status": "sent", "priority": 3})
assert ticket.status is Status.SENT and ticket.priority is Priority.HIGH
assert ticket.dump() == {"channel": "web", "status": "sent", "priority": 3}
```

* **Strict values.** `"3"`, `3.0` and `True` are not `3`. A wrong type answers
  `type`; a value outside the list answers `one_of`, and the message lists the
  accepted values, never member names.
* **One kind.** All values are `str` or all `int` (64-bit). A mixed enum or
  `Literal`, `None` inside a `Literal` (write `Literal["a"] | None`), a `Flag`,
  an empty enum, an enum member inside a `Literal` and other value types raise
  `RegistrationError`. `Field(...)` constraints are refused: the type is the
  list.
* **Construction** takes members for an enum field and the listed values for a
  `Literal`; `dump()` writes values, `to_dict()` keeps members.
* **Text sources** read an `IntEnum` or `int` `Literal` like an `int`.
* **Models.** An `Enum` column stores the value; a `Literal` column has no SQL
  type: use an enum or `str`/`int` in the model and the `Literal` in the
  `Schema`.
* **OpenAPI:** `type` `string` or `integer` with an inline `enum` list.

## Dates, durations, decimals and bytes

These travel as JSON strings and validate identically on both lanes.

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
assert booking.dump()["price"] == "19.90"
```

| Type | Accepted input | Output | Error code |
| --- | --- | --- | --- |
| `date` | `YYYY-MM-DD`, a real date in years 0001–9999 | same text | `date` |
| `time` | `HH:MM:SS[.f…]` (1–6 fraction digits) with `Z` or `±HH:MM` | `isoformat()` | `time`; `time_tz` without offset |
| `timedelta` | ISO 8601 `[-]P[nD][T[nH][nM][n[.f…]S]]`; no years, months or weeks | `PT1H30M`, `-PT0.5S` | `duration`; `duration_range` |
| `Decimal` | string `-?(0\|[1-9][0-9]*)(\.[0-9]+)?` or integer; no floats, booleans, exponents, `NaN`, infinities | fixed-point string keeping the scale (`"0.10"`) | `decimal` |
| `bytes` | standard padded base64, no whitespace, not URL-safe | padded base64 | `bytes` |

* **Times need an offset** on the wire, like a `datetime`; `from_model()`
  accepts naive values. A `date` field refuses a `datetime`.
* **`Decimal` is exact.** `ge`, `le`, `gt`, `lt` take an `int` or `Decimal`.
  `max_digits` (default 64, at most 1000) counts all digits, `decimal_places`
  those after the point; checked in the order `max_digits`, `decimal_places`,
  `max_whole_digits`, bounds, `multiple_of`.
* **`bytes` size.** Lengths count decoded bytes. Without `max_length`, client
  input is capped at 65 536 bytes (`hinoki.fieldtypes.MAX_BYTES_LENGTH`);
  model-derived fields are not capped. `pattern` matches the decoded bytes read
  as Latin-1.
* **Output.** The JSON and XML encoders write these values in their wire form
  anywhere, including in a plain dict or at the top level; a `JSONRenderer`
  with a `default` hook passes them to the hook first outside a `Schema`.
* **OpenAPI:** `format` `date`, `time`, `duration`, `decimal` (with a
  `pattern`) and `byte` (`contentEncoding: base64`).

## Internet addresses, `Email`, `Url` and `Slug`

```python
from ipaddress import IPv4Address, IPv4Network

from hinoki import Email, Schema, Url


class Allowlist(Schema):
    owner: Email
    homepage: Url | None = None
    source: IPv4Address
    subnet: IPv4Network | None = None


rule = Allowlist.validate(
    {"owner": "ops@example.com", "source": "10.0.0.7", "subnet": "10.0.0.0/24"}
)
assert rule.source == IPv4Address("10.0.0.7")
```

* `IPv4Address`, `IPv6Address`, `IPv4Network`, `IPv6Network` read strict ASCII
  text and become the object (codes `ipv4`, `ipv6`, `ipv4network`,
  `ipv6network`). IPv4 refuses leading zeros, IPv6 refuses zone ids, networks
  refuse host bits (`10.0.0.7/24`). `Field(...)` is refused. A union such as
  `IPv4Address | IPv6Address` takes either.
* `Email`, `Url` and `Slug` are `Annotated[str, Field(format=...)]` shortcuts;
  `Email` checks syntax, not deliverability. They work as model columns.
* **Models** store addresses as text and check the class before a write.

## `Secret` and `Path`

```python
from pathlib import Path
from typing import Annotated

from hinoki import Field, Schema, Secret


class Account(Schema):
    password: Annotated[Secret, Field(min_length=8)]
    home: Path


account = Account.validate({"password": "correct horse", "home": "/srv/ann"})
assert account.password.reveal() == "correct horse"
assert account.dump() == {"password": "**********", "home": "/srv/ann"}
```

* `Secret` shows `**********` in `str`, `dump()` and responses (its `repr` is
  `Secret('**********')`); only `reveal()` returns the text, and errors never
  repeat it. Construction takes a `str` only; two secrets compare in constant
  time. It is the same type as `settings.Secret`, and `JWTKey(key=...)` and
  `SessionSettings(csrf_secret=...)` accept it.
* `Path` reads a non-empty string without NUL (at most 4096 characters without
  `max_length`) and never touches the file system; code `path`.
* Both are served by the Python lane by design and have no model column type.
* **OpenAPI:** `format: path`; a secret is `format: password`, `writeOnly`.

## See also

- [Collections and unions](schema-collections.md)
- [Validate input](schema-validation.md)
- [OpenAPI](openapi.md)
