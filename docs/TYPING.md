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
| Decorator composition | User decorators should preserve signatures with `functools.wraps`, `ParamSpec` and `TypeVar`. |

## ORM method signatures

Model construction and common instance operations are typed. Some ORM methods that accept arbitrary model field names still have broad keyword signatures, so a checker may not catch a misspelled field passed to these methods:

```python
task = Task(title="Write docs")  # constructor fields and values are checked
await task.asave()
```

`Model.acreate(**values)` and some filter/update keyword calls cannot currently verify each field name against the model class. Use the constructor plus `asave()` when you want field-level checking on insertion. This limitation does not affect runtime validation of model fields or database constraints.

The Python and Rust packages include typing metadata and the native module has a stub. See the [API reference](reference/index.md) for the public signatures.
