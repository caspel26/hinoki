"""Modules: named units with explicit dependencies, APIs and lifespan.

Declaring a module opens nothing. Resources are acquired in ``lifespan``,
which runs once per worker, in dependency order, and is unwound in reverse.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import ClassVar, TypeVar

from hinoki.api import API
from hinoki.errors import ModuleGraphError
from hinoki.runtime.tasks import ManagedTasks

T = TypeVar("T")


class ModuleContext:
    """Per-worker context handed to module lifespans.

    ``provide``/``get`` is a typed, explicit registry — not a global service
    locator: one instance per worker, keyed by type.
    """

    def __init__(self, settings: object | None = None) -> None:
        self.settings = settings
        self.tasks = ManagedTasks()
        self._provided: dict[type[object], object] = {}

    def reset(self) -> None:
        """Forget provided resources and tasks; called between lifespans."""
        self.tasks = ManagedTasks()
        self._provided.clear()

    def provide(self, kind: type[T], value: T) -> None:
        if kind in self._provided:
            raise ModuleGraphError(f"{kind.__name__} is already provided")
        self._provided[kind] = value

    def find(self, kind: type[T]) -> T | None:
        """The provided value of this type, or ``None``."""
        value = self._provided.get(kind)
        return value if isinstance(value, kind) else None

    def get(self, kind: type[T]) -> T:
        try:
            value = self._provided[kind]
        except KeyError:
            raise LookupError(f"{kind.__name__} was not provided") from None
        if not isinstance(value, kind):
            raise TypeError(f"{kind.__name__} registry entry has the wrong type")
        return value


@asynccontextmanager
async def _no_lifespan() -> AsyncIterator[None]:
    yield


class Module:
    """Base class for modules. Override ``lifespan`` to own resources."""

    name: ClassVar[str] = ""
    requires: ClassVar[tuple[type[Module], ...]] = ()
    apis: ClassVar[tuple[type[API], ...]] = ()

    @classmethod
    def from_settings(cls, settings: object) -> Module:  # noqa: ARG003 - for overrides
        """Build the module from the application's settings. Override it to make
        ``Hinoki(modules=[YourModule])`` work; the default says it is not supported."""
        raise TypeError(
            f"{cls.__name__} cannot be built from settings; pass an instance, "
            f"{cls.__name__}(...), or add a from_settings classmethod"
        )

    def lifespan(
        self,
        ctx: ModuleContext,  # noqa: ARG002 - part of the overridable interface
    ) -> AbstractAsyncContextManager[None]:
        return _no_lifespan()


class ModuleGraph:
    """Dependency graph of installed modules."""

    def __init__(self, modules: Sequence[Module]) -> None:
        self.modules = modules
        self._ordered: list[Module] = []
        self._done: set[str] = set()
        self._visiting: list[str] = []

    def ordered(self) -> list[Module]:
        """Dependencies first, stable in install order.

        Raises ``ModuleGraphError`` for empty/duplicate names, missing
        requirements and cycles.
        """
        self._check_names()
        for module in self.modules:
            self._visit(module)
        return self._ordered

    def _check_names(self) -> None:
        seen: set[str] = set()
        for module in self.modules:
            if not module.name:
                raise ModuleGraphError(f"{type(module).__name__} has no name")
            if module.name in seen:
                raise ModuleGraphError(f"duplicate module name {module.name!r}")
            seen.add(module.name)

    def _provider(self, owner: Module, wanted: type[Module]) -> Module:
        for candidate in self.modules:
            if type(candidate) is wanted:
                return candidate
        raise ModuleGraphError(
            f"module {owner.name!r} requires {wanted.__name__}, which is not installed"
        )

    def _visit(self, module: Module) -> None:
        if module.name in self._done:
            return
        if module.name in self._visiting:
            start = self._visiting.index(module.name)
            cycle = [*self._visiting[start:], module.name]
            raise ModuleGraphError("dependency cycle: " + " -> ".join(cycle))
        self._visiting.append(module.name)
        for wanted in module.requires:
            self._visit(self._provider(module, wanted))
        self._visiting.pop()
        self._done.add(module.name)
        self._ordered.append(module)


def order_modules(modules: Sequence[Module]) -> list[Module]:
    return ModuleGraph(modules).ordered()
