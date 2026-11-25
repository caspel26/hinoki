"""A static proof that an async handler awaits only native database calls.

The native lane runs a handler outside an asyncio task, so a handler that
needs one (``asyncio.timeout``, ``TaskGroup``, ``current_task``) would fail
half way, after queries already ran. The lane therefore takes only handlers
this module proves safe, and anything it cannot prove stays on the Python
path. A proof looks at the handler's own source, which must be a plain
function, and accepts exactly these awaits, with names resolved to the real
objects when the application starts::

    await Model.aget(...)             # also aget_or_none
    await Model.filter(...).afirst()  # also aget, acount, aexists
    await Model.filter(...).order_by(...).limit(...)   # the rows
    await Model.filter(...).select_related(...).aget()  # joined in one query

``Model`` must be a ``hinoki.Model`` and every link of the chain a stock
method. Any other await, ``async with``, ``async for``, async comprehension,
nested ``async def`` or ``yield`` means no proof, and so does a direct use of
``asyncio`` or ``anyio`` in the handler body (``current_task`` returns nothing
outside a task). The proof assumes the ORM and the model classes are not
replaced after startup, and it does not look into functions the handler calls:
their own use of the current task is outside its reach.
"""

import ast
import inspect
import textwrap
import types
from collections.abc import Callable
from typing import Any

from hinoki.events import Created, Deleted, EventBus, Updated
from hinoki.orm.model import Model

MODEL_STARTS = frozenset({"filter", "all"})
MODEL_TERMINALS = frozenset({"aget", "aget_or_none", "aget_as", "aget_or_none_as"})
MODEL_WRITES = frozenset(
    {
        "acreate",
        "aupdate",
        "aupdate_where",
        "adelete",
        "abulk_create",
        "abulk_update",
        "abulk_delete",
    }
)
QUERYSET_BUILDERS = frozenset(
    {"filter", "select_related", "prefetch_related", "order_by", "limit", "offset"}
)
QUERYSET_TERMINALS = frozenset(
    {
        "aget",
        "afirst",
        "acount",
        "aexists",
        "aproject",
        "apaginate_as",
        "aget_as",
        "aget_or_none_as",
    }
)
QUERYSET_WRITES = frozenset({"aupdate", "adelete"})
# The model event a write emits; ``aupdate`` and ``aupdate_where`` emit none.
MODEL_WRITE_EVENTS: dict[str, type] = {
    "acreate": Created,
    "abulk_create": Created,
    "abulk_update": Updated,
    "adelete": Deleted,
    "abulk_delete": Deleted,
}
QUERYSET_WRITE_EVENTS: dict[str, type] = {"adelete": Deleted}
# What a write calls on the model: none of it may be replaced.
WRITE_PATH = (
    "insert",
    "insert_observed",
    "write_insert",
    "write_update",
    "save_with",
    "save_observed",
    "event_transaction",
    "asave",
    "from_schema",
    "merged_values",
    "load_stored",
    "validate_values",
    "hook_transaction",
    "run_hooks",
    "bulk_instances",
    "insert_all",
    "saved_rows",
    "save_all",
)
TASK_MODULES = frozenset({"asyncio", "_asyncio", "anyio"})
TASK_APIS = frozenset({"asyncio", "anyio", "current_task", "all_tasks"})
MAX_HELPER_DEPTH = 8
MISSING = object()
UNSUPPORTED = (
    ast.AsyncWith,
    ast.AsyncFor,
    ast.AsyncFunctionDef,
    ast.Yield,
    ast.YieldFrom,
)


class ProofError(Exception):
    """The reason a handler is not provably native."""


def function_tree(func: Callable[..., Any]) -> tuple[types.FunctionType, ast.AST]:
    """The handler and the syntax tree of its own source."""
    if not isinstance(func, types.FunctionType) or not inspect.iscoroutinefunction(
        func
    ):
        raise ProofError("not a plain async function")
    if inspect.unwrap(func) is not func:
        raise ProofError("wrapped by another decorator")
    try:
        source = textwrap.dedent(inspect.getsource(func))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError):
        raise ProofError("its source is not available") from None
    definition = tree.body[0] if tree.body else None
    if not isinstance(definition, ast.AsyncFunctionDef):
        raise ProofError("its source is not one async function")
    return func, definition


def resolve(func: types.FunctionType, node: ast.expr) -> object:
    """The object a dotted name stands for, from the closure or the module."""
    if isinstance(node, ast.Attribute):
        owner = resolve(func, node.value)
        if not isinstance(owner, types.ModuleType):
            raise ProofError(f"{node.attr} is read from something that is not a module")
        try:
            return getattr(owner, node.attr)
        except AttributeError:
            raise ProofError(f"{node.attr} is not defined yet") from None
    if not isinstance(node, ast.Name):
        raise ProofError("an await starts from something other than a name")
    code = func.__code__
    if node.id in code.co_varnames or node.id in code.co_cellvars:
        raise ProofError(f"{node.id} is a local name")
    if node.id in code.co_freevars:
        cells = dict(zip(code.co_freevars, func.__closure__ or (), strict=False))
        try:
            return cells[node.id].cell_contents
        except (KeyError, ValueError):
            raise ProofError(f"{node.id} is not bound yet") from None
    found = func.__globals__.get(node.id, MISSING)
    if found is MISSING:
        raise ProofError(f"{node.id} is not a module-level name")
    return found


def chain_of(operand: ast.expr) -> tuple[ast.expr, list[str]]:
    """The root expression and the method names of ``root.a(...).b(...)``."""
    links: list[str] = []
    current = operand
    while isinstance(current, ast.Call) and isinstance(current.func, ast.Attribute):
        links.insert(0, current.func.attr)
        current = current.func.value
    return current, links


def is_stock(model: type[Model], name: str) -> bool:
    stock = getattr(Model, name, None)
    own = getattr(model, name, None)
    return getattr(own, "__func__", own) is getattr(stock, "__func__", stock)


def check_write(model: type[Model], bus: EventBus | None, kind: type | None) -> None:
    """A write is native only for a model whose write path is stock and that
    has no hook: a hook is application code that may await anything. A handler
    for the event the write emits (``kind``) is application code too."""
    for name in WRITE_PATH:
        if not is_stock(model, name):
            raise ProofError(f"{model.__name__}.{name} is overridden")
    if model.write_hooks():
        raise ProofError(f"{model.__name__} has write hooks")
    if bus is not None and kind is not None and bus.registered(kind, model):
        raise ProofError(f"{model.__name__} has {kind.__name__} event handlers")


def check_chain(model: type[Model], links: list[str], bus: EventBus | None) -> None:
    first, rest = links[0], links[1:]
    if not is_stock(model, first):
        raise ProofError(f"{model.__name__}.{first} is overridden")
    if first in MODEL_TERMINALS | MODEL_WRITES:
        if rest:
            raise ProofError(f"{first} is followed by more calls")
        if first in MODEL_WRITES:
            check_write(model, bus, MODEL_WRITE_EVENTS.get(first))
        return
    if first not in MODEL_STARTS:
        raise ProofError(f"{model.__name__}.{first} is not a native query")
    check_steps(model, rest, bus)


def check_steps(model: type[Model], steps: list[str], bus: EventBus | None) -> None:
    for name in steps[:-1]:
        if name not in QUERYSET_BUILDERS:
            raise ProofError(f"{name} is not a native query step")
    if not steps:
        return
    last = steps[-1]
    if last not in QUERYSET_BUILDERS | QUERYSET_TERMINALS | QUERYSET_WRITES:
        raise ProofError(f"{last} is not a native query step")
    if last in QUERYSET_WRITES:
        check_write(model, bus, QUERYSET_WRITE_EVENTS.get(last))


def resolve_helper(
    func: types.FunctionType, node: ast.expr
) -> types.FunctionType | None:
    """Resolve a direct call ``helper(...)`` to a module-local plain async
    function, or ``None`` when ``node`` is not a call to a defined function."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return None
    name = node.func.id
    code = func.__code__
    if name in code.co_varnames or name in code.co_cellvars or name in code.co_freevars:
        return None
    found = func.__globals__.get(name, MISSING)
    if found is MISSING or not isinstance(found, types.FunctionType):
        return None
    if found.__globals__ is not func.__globals__:
        raise ProofError(f"{name} is not defined in this module")
    if found.__code__.co_freevars:
        raise ProofError(f"{name} captures closure variables")
    return found


def check_helper(
    helper: types.FunctionType,
    bus: EventBus | None,
    depth: int,
    seen: frozenset[types.FunctionType],
    proved: set[types.FunctionType],
) -> None:
    """Prove a module-local async helper under the same rules, with bounded
    depth and cycle detection."""
    if helper in seen:
        raise ProofError(f"{helper.__name__} is recursive")
    if depth >= MAX_HELPER_DEPTH:
        raise ProofError("helper call depth exceeded")
    if helper not in proved:
        _prove(helper, bus, depth + 1, seen, proved)
        proved.add(helper)


def check_await(
    func: types.FunctionType,
    node: ast.Await,
    bus: EventBus | None,
    depth: int,
    seen: frozenset[types.FunctionType],
    proved: set[types.FunctionType],
) -> None:
    root, links = chain_of(node.value)
    if not links:
        helper = resolve_helper(func, node.value)
        if helper is None:
            raise ProofError("an await is not a call on a model")
        check_helper(helper, bus, depth, seen, proved)
        return
    target = resolve(func, root)
    if not (isinstance(target, type) and issubclass(target, Model)):
        raise ProofError("an await is not on a Model class")
    check_chain(target, links, bus)


def uses_task_api(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id in TASK_APIS
    return isinstance(node, ast.Attribute) and node.attr in TASK_APIS


def names_task_api(func: types.FunctionType, node: ast.AST) -> bool:
    """A name bound to asyncio or anyio, whatever it is called here
    (``from asyncio import current_task as owner``)."""
    if not isinstance(node, ast.Name):
        return False
    try:
        found = resolve(func, node)
    except ProofError:
        return False
    owner = found.__name__ if isinstance(found, types.ModuleType) else None
    owner = owner or getattr(found, "__module__", None) or ""
    return owner.split(".")[0] in TASK_MODULES


def imports_task_api(node: ast.AST) -> bool:
    if isinstance(node, ast.ImportFrom):
        return (node.module or "").split(".")[0] in TASK_MODULES
    if isinstance(node, ast.Import):
        return any(a.name.split(".")[0] in TASK_MODULES for a in node.names)
    return False


def comprehension_is_async(node: ast.AST) -> bool:
    return isinstance(node, ast.comprehension) and bool(node.is_async)


def _prove(
    func: Callable[..., Any],
    bus: EventBus | None,
    depth: int,
    seen: frozenset[types.FunctionType],
    proved: set[types.FunctionType],
) -> None:
    function, definition = function_tree(func)
    active = seen | {function}
    for node in ast.walk(definition):
        if node is definition:
            continue
        if isinstance(node, UNSUPPORTED) or comprehension_is_async(node):
            raise ProofError(f"it uses {type(node).__name__}")
        if (
            uses_task_api(node)
            or imports_task_api(node)
            or names_task_api(function, node)
        ):
            raise ProofError("it uses asyncio directly")
        if isinstance(node, ast.Await):
            check_await(function, node, bus, depth, active, proved)


def prove(func: Callable[..., Any], bus: EventBus | None = None) -> None:
    """Raise :class:`ProofError` with the reason unless ``func`` is provably
    native. With the application's ``bus``, a write whose model event has a
    handler is not."""
    _prove(func, bus, 0, frozenset(), set())


def native_refusal(func: Callable[..., Any], bus: EventBus | None = None) -> str | None:
    """Why ``func`` stays on the Python path, or ``None`` when it is provable."""
    try:
        prove(func, bus)
    except ProofError as reason:
        return str(reason)
    return None
