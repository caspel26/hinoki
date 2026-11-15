# Type checking Hinoki applications

Hinoki's public APIs use Python annotations so a type checker can catch mistakes in handlers, decorators and schema construction. The repository is checked with mypy in strict mode; other checkers and IDEs have not been evaluated systematically.

Run the project's check from its checkout:

```sh
uv run --locked mypy
```

## Typed surfaces

| Contract | Result |
| --- | --- |
| HTTP decorators | Preserve a handler's parameter and return types. Invalid decorator options such as `transaction="yes"` are type errors. |
| `Schema` classes | Generated constructors check required, unknown and mistyped fields; `Field(default_factory=...)` preserves the value type. |
| Application and route groups | `Hinoki.include`, `API` classes and `RouteGroup` provide typed declarations. Unknown include items fail at app construction. |
| Authentication context | Annotate a parameter as `Caller[User]` to receive the typed caller and application user. |
| Decorator composition | User decorators should preserve signatures with `functools.wraps`, `ParamSpec` and `TypeVar`. For standalone handlers the final decorated function is dispatched. With `RouteGroup`, put the HTTP decorator outermost so it captures the final handler. |

## ORM method signatures

Model construction and common instance operations are typed. Some ORM methods that accept arbitrary model field names still have broad keyword signatures, so a checker may not catch a misspelled field passed to these methods:

```python
task = Task(title="Write docs")  # constructor fields and values are checked
await task.asave()
```

Fields inherited from an abstract model are part of the checked constructor. Fields that come from a plain (non-model) mixin exist at run time but are not constructor arguments for a type checker; use an abstract model when you need them checked.

`Model.acreate(**values)` and some filter/update keyword calls cannot currently verify each field name against the model class. Use the constructor plus `asave()` when you want field-level checking on insertion. This limitation does not affect runtime validation of model fields or database constraints.

The return types of the write methods are checked: `aupdate` returns the model, `aupdate_where` and `QuerySet.aupdate` a list of it, and with a literal `returning=False` each returns `int` ([writes that do not read the row back](models.md#writes-that-do-not-read-the-row-back)). A `returning` value that is a plain `bool` gives the union of both. `acreate` and `insert` return the model either way. Unpacking a dictionary into `aupdate(**values)` or `update(**values)` gives `Any`, because the dictionary could hold `returning`; pass `returning=True` explicitly to keep the model type.

The Python package includes typing metadata, and the compiled `hinoki.core` module has a matching stub. See the [API reference](reference/index.md) for the public signatures.
