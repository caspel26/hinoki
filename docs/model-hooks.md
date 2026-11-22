---
status: implemented
related:
  - queries.md
  - transactions.md
  - crud.md
---

# Lifecycle hooks

A model can react to its own writes with `before_save`, `before_create`,
`after_create` and `after_save`. Each is an ordinary method, `def` or
`async def`, taking only `self`. A model without hooks pays nothing.

```python
# notes/apps/billing/models.py (continued)
import logging

log = logging.getLogger(__name__)


class Invoice(Model):
    ...  # fields as in Define models

    def before_save(self) -> None:
        if self.was_changed("reference"):
            self.reference = self.reference.strip().upper()

    async def after_create(self) -> None:
        self.after_commit(lambda: log.info("invoice %s created", self.id))
```

## Order

| Write | Hooks |
| --- | --- |
| insert (`acreate`, `insert`, `asave` of a new instance) | `before_save`, `before_create`, write, `after_create`, `after_save` |
| update (`asave` of a loaded instance, `aupdate_from`) | `before_save`, write, `after_save` |

`before_*` hooks may change the values about to be written. `after_*` hooks see
the stored row (generated keys, database defaults, `@db.timestamps()`). An update
with no changed column after `before_save` writes nothing and skips
`after_save`. To extend an inherited hook, call `super()`.

## Instance state

| Member | Meaning |
| --- | --- |
| `is_new` | `True` until the row is written or loaded; still `True` inside the hooks of an insert |
| `is_dirty` | `True` for a new instance or when a column differs from the stored row |
| `changes` | `{column: Change(old, new)}`; a new instance lists every column with `old=UNSET` |
| `was_changed("reference")` | whether one column differs; an unknown name raises `FieldError` |

Stored values are refreshed after the `after_*` hooks, so `changes` still shows
this write's changes while they run.

## Transactions and `after_commit`

With any hook defined, the write runs in a transaction (a savepoint inside an
open one): an exception in a hook rolls the write back and reaches the caller.
A hook that opens its own `db.atomic()` fails with `DatabaseError` (`nested
atomic scopes (savepoints) must be opened by the task that owns the
transaction`) when a sync write runs inside a sync handler's
`with db.atomic():`; from async code it nests as a savepoint.
`after_*` hooks run before the commit. Work that must happen only after it goes
through `self.after_commit(callback)`, a sync or async callable without
arguments. Callbacks run in order after the outermost commit; a rollback drops
them; an exception in one is logged and does not undo the commit. See
[transactions](transactions.md#after-commit-callbacks).

## Limits

* Hooks belong to instances: `QuerySet.aupdate`, `adelete`, `Model.aupdate`,
  `aupdate_where` and raw SQL do not run them.
* Events complete hooks for code that is not the model: `Created`, `Updated`
  and `Deleted` [model events](events.md#model-events) fire from the same
  writes (and from `adelete`), with handlers registered per application
  instead of overridden on the class. `after_commit` on a model and
  `db.after_commit` keep working unchanged.
* Sync hooks run inline on the event loop: keep them short and use
  `async def` for I/O.
* `is_new`, `is_dirty`, `changes`, `was_changed`, `after_commit` and the four
  hook names are reserved field names.
* From a helper task of a transaction (the sync model methods of a sync
  handler), a hooked write joins that transaction; an error still rolls the
  whole transaction back.
* A hook assigned to the class after creation is used from the next write.
* Generated CRUD and the admin's create and edit forms save through the same
  methods, so hooks run there too; deletes run none
  ([CRUD](crud.md#model-hooks-and-kiso)).

## See also

- [Query and write rows](queries.md)
- [Transactions](transactions.md)
