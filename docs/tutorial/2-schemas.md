# 2. Schemas

A model describes a table; a `Schema` describes what crosses the API. A column reaches a client only when a schema names it.

```python
from typing import Annotated

from tracker.models import Task
from hinoki import Field, Schema, derive


class TaskIn(Schema):
    """What a client may set. The owner is the caller, never a field."""

    title: Annotated[str, Field(min_length=1, max_length=120)]


@derive(exclude=("owner_id",))
class TaskOut(Schema, model=Task):
    """Every column of a task except its owner (derived from the model)."""
```

* `TaskIn` is strict: no coercion, no unknown keys, every problem reported with its path. Validation runs in Rust before your code does.
* `TaskOut` takes the columns of `Task`: types and limits come from the model, and `exclude=("owner_id",)` keeps `owner_id` out. Relations are never derived; you name them on purpose.
* Because the owner is not a field of `TaskIn`, a client cannot choose it. If it were, `Task.acreate(data, owner_id=me)` would still win (server values beat the client's), and a schema can never carry the primary key.

When one property can contain more than one shape, put an explicit tagged union inside a wrapper Schema. Every variant has a required tag with a distinct `one_of` value and the same wire alias:

```python
from typing import Annotated

from hinoki import Field, Schema


class Cat(Schema):
    kind: Annotated[str, Field(alias="type", one_of=("cat",))]
    name: str


class Dog(Schema):
    kind: Annotated[str, Field(alias="type", one_of=("dog",))]
    walks: int


Pet = Annotated[Cat | Dog, Field(discriminator="kind")]


class PetRequest(Schema):
    pet: Pet
```

`PetRequest.validate({"pet": {"type": "cat", "name": "Mugi"}})` builds a `Cat`; the same tag also produces an OpenAPI `oneOf` with a discriminator mapping. See [discriminated unions](../schemas.md#discriminated-unions) for request errors, lists, nullable fields and limits.

More: [Schemas](../schemas.md), [model-derived schemas](../model-schemas.md).

Next: [3. Operations](3-operations.md). Validation patterns continue in [7. Validate input](7-validation.md).
