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

from hinoki.orm.model import Model

MODEL_STARTS = frozenset({"filter", "all"})
MODEL_TERMINALS = frozenset({"aget", "aget_or_none"})
QUERYSET_BUILDERS = frozenset({"filter", "order_by", "limit", "offset"})
QUERYSET_TERMINALS = frozenset({"aget", "afirst", "acount", "aexists"})
TASK_MODULES = frozenset({"asyncio", "_asyncio", "anyio"})
TASK_APIS = frozenset({"asyncio", "anyio", "current_task", "all_tasks"})
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


def check_chain(model: type[Model], links: list[str]) -> None:
    first, rest = links[0], links[1:]
    stock = getattr(Model, first, None)
    own = getattr(model, first, None)
    if getattr(own, "__func__", own) is not getattr(stock, "__func__", stock):
        raise ProofError(f"{model.__name__}.{first} is overridden")
    if first in MODEL_TERMINALS:
        if rest:
            raise ProofError(f"{first} is followed by more calls")
        return
    if first not in MODEL_STARTS:
        raise ProofError(f"{model.__name__}.{first} is not a native query")
    for name in rest[:-1]:
        if name not in QUERYSET_BUILDERS:
            raise ProofError(f"{name} is not a native query step")
    if rest and rest[-1] not in QUERYSET_BUILDERS | QUERYSET_TERMINALS:
        raise ProofError(f"{rest[-1]} is not a native query step")


def check_await(func: types.FunctionType, node: ast.Await) -> None:
    root, links = chain_of(node.value)
    if not links:
        raise ProofError("an await is not a call on a model")
    target = resolve(func, root)
    if not (isinstance(target, type) and issubclass(target, Model)):
        raise ProofError("an await is not on a Model class")
    check_chain(target, links)


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


def prove(func: Callable[..., Any]) -> None:
    """Raise :class:`ProofError` with the reason unless ``func`` is provably native."""
    function, definition = function_tree(func)
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
            check_await(function, node)


def native_refusal(func: Callable[..., Any]) -> str | None:
    """Why ``func`` stays on the Python path, or ``None`` when it is provable."""
    try:
        prove(func)
    except ProofError as reason:
        return str(reason)
    return None
