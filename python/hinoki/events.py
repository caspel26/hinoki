"""Typed events with explicit handlers.

An event is a class (a ``Schema`` or a frozen dataclass). A ``Handlers`` collection
registers functions for event types in one of two phases named after what they
may do: ``in_transaction`` handlers run inside the writer's transaction and can
fail it; ``after_commit`` handlers run once the outermost transaction has
committed and can cancel nothing. The application lists its collections in
``Hinoki(events=[...])`` (or ``manifest_events()``); nothing registers by import.
Delivery is inside one worker process: there is no queue and no retry.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import inspect
import logging
import typing
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, cast

from hinoki import context
from hinoki.errors import EventError, EventRegistrationError, EventTimeout
from hinoki.settings import EventsSettings

if TYPE_CHECKING:
    from hinoki.db import ActiveTransaction

log = logging.getLogger(__name__)

EventKey = tuple[type, type | None]

# The index the native recorder takes for each outcome.
OUTCOMES = {"ok": 0, "error": 1, "timeout": 2}


class EventRecorder(Protocol):
    def record(self, slot: int, outcome: int, seconds: float) -> None: ...


class Phase(enum.Enum):
    IN_TRANSACTION = "in_transaction"
    AFTER_COMMIT = "after_commit"


@dataclass(frozen=True, slots=True)
class Registration:
    """One handler as registered: where it runs, for which event, in what order."""

    handler: Callable[[Any], object]
    phase: Phase
    key: EventKey
    name: str
    order: int = 0
    collection: str = ""

    @property
    def is_async(self) -> bool:
        return inspect.iscoroutinefunction(self.handler)

    @property
    def event_name(self) -> str:
        return key_name(self.key)


def key_name(key: EventKey) -> str:
    kind, subject = key
    return (
        kind.__qualname__
        if subject is None
        else (f"{kind.__qualname__}[{subject.__qualname__}]")
    )


def event_key(kind: object) -> EventKey:
    """``(event class, subject class or None)`` for ``Created`` or ``Created[Note]``."""
    origin = typing.get_origin(kind)
    if origin is None:
        if not isinstance(kind, type):
            raise EventRegistrationError(f"{kind!r} is not an event class")
        return (kind, None)
    args = typing.get_args(kind)
    if not isinstance(origin, type) or len(args) != 1 or not isinstance(args[0], type):
        raise EventRegistrationError(
            f"{kind!r} is not an event class or an event class of one class"
        )
    return (origin, args[0])


def handler_name(handler: Callable[..., object]) -> str:
    return f"{handler.__module__}.{handler.__qualname__}"


def check_handler(handler: Callable[..., object], key: EventKey, phase: Phase) -> None:
    """Refuse a handler that is untyped, takes the wrong event or cannot be
    timed out in its phase."""
    name = handler_name(handler)
    if not callable(handler):
        raise EventRegistrationError(f"{name} is not callable")
    if phase is Phase.IN_TRANSACTION and not inspect.iscoroutinefunction(handler):
        raise EventRegistrationError(
            f"{name}: an in_transaction handler must be 'async def' so that its "
            "time limit can be enforced inside the transaction"
        )
    parameters = list(inspect.signature(handler).parameters.values())
    if len(parameters) != 1 or parameters[0].kind not in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    ):
        raise EventRegistrationError(f"{name} must take exactly one event parameter")
    declared = event_key(handler_parameter(handler, parameters[0].name, name))
    if declared[0] is not key[0] or (key[1] is not None and declared[1] is not key[1]):
        raise EventRegistrationError(
            f"{name}: the parameter is annotated {key_name(declared)} but the "
            f"handler is registered for {key_name(key)}"
        )


def handler_parameter(
    handler: Callable[..., object], parameter: str, name: str
) -> object:
    try:
        hints = typing.get_type_hints(handler)
    except Exception as error:
        raise EventRegistrationError(
            f"{name}: cannot resolve its annotations ({error})"
        ) from error
    if parameter not in hints:
        raise EventRegistrationError(f"{name}: the event parameter has no type")
    return hints[parameter]


class Handlers:
    """A collection of event handlers, listed on the application to take effect.

    ``@handlers.in_transaction(Event)`` and ``@handlers.after_commit(Event)``
    register the decorated function, which is returned unchanged so a test can
    call it directly. The event type is ``Event`` or, for events about one
    class, ``Event[Note]``; the handler's parameter must be annotated with the
    same type. Registration order is execution order.
    """

    def __init__(self, name: str = "") -> None:
        self.name = name
        self._items: list[Registration] = []

    @property
    def registrations(self) -> tuple[Registration, ...]:
        return tuple(self._items)

    def in_transaction[E](
        self, kind: type[E]
    ) -> Callable[[Callable[[E], Awaitable[None]]], Callable[[E], Awaitable[None]]]:
        """Run inside the transaction of the write that emitted the event. An
        exception or a timeout rolls the write back and reaches its caller."""
        return self._register(kind, Phase.IN_TRANSACTION)

    def after_commit[E](
        self, kind: type[E]
    ) -> Callable[
        [Callable[[E], Awaitable[None] | None]],
        Callable[[E], Awaitable[None] | None],
    ]:
        """Run after the outermost transaction commits (at once when none is
        open). A failure is logged and counted; it cancels nothing."""
        return self._register(kind, Phase.AFTER_COMMIT)

    def _register[F: Callable[..., object]](
        self, kind: object, phase: Phase
    ) -> Callable[[F], F]:
        key = event_key(kind)

        if phase is Phase.IN_TRANSACTION and not getattr(
            key[0], "has_transaction", True
        ):
            raise EventRegistrationError(
                f"{key_name(key)} is emitted outside any transaction: register "
                "it with after_commit"
            )

        def decorate(handler: F) -> F:
            check_handler(handler, key, phase)
            self._items.append(
                Registration(
                    handler, phase, key, handler_name(handler), collection=self.name
                )
            )
            return handler

        return decorate


@dataclass(frozen=True, slots=True)
class Created[M]:
    """A model row was inserted by ``insert`` (``acreate``, ``asave`` of a new
    instance, ``abulk_create``, generated CRUD create). ``instance`` is the
    stored instance."""

    subject: type[M]
    instance: M


@dataclass(frozen=True, slots=True)
class Updated[M]:
    """A model instance was saved with changes (``asave``, ``aupdate_from``,
    ``abulk_update``, generated CRUD update). ``changed`` names the columns
    that differed from the stored row before the write."""

    subject: type[M]
    instance: M
    changed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Deleted[M]:
    """A DELETE statement removed ``count`` rows of the model (``adelete``,
    ``abulk_delete``, generated CRUD delete). No key or row is loaded."""

    subject: type[M]
    count: int


@dataclass(frozen=True, slots=True)
class AuthRejected:
    """A route's access policy refused a request (HTTP 401 or 403).

    ``reason`` is ``"missing"`` (no credentials), ``"invalid"`` (credentials the
    provider rejected) or ``"forbidden"`` (a caller without a required
    permission). ``route`` is the path template (``/notes/{note_id}``), never the
    requested path; ``method`` is the HTTP method, or ``WEBSOCKET``. The payload
    holds no credential, caller, address or query string. Delivered through
    ``EventBus.notify``, so it needs ``after_commit`` handlers."""

    has_transaction: ClassVar[bool] = False

    reason: str
    route: str
    method: str


@dataclass(frozen=True, slots=True)
class RateLimited:
    """A route's rate-limit policy refused a request (HTTP 429).

    ``policy`` is the class name of the route's policy, ``retry_after`` the whole
    seconds the client was told to wait. The payload holds no key, caller,
    address or path. Delivered through ``EventBus.notify``, so it needs
    ``after_commit`` handlers."""

    has_transaction: ClassVar[bool] = False

    policy: str
    route: str
    method: str
    retry_after: int


# Shared by the helpers of one emit: how deep the handlers are nested.
_depth: ContextVar[int] = ContextVar("hinoki_event_depth", default=0)
_captured: ContextVar[list[object] | None] = ContextVar(
    "hinoki_event_capture", default=None
)
_muted: ContextVar[bool] = ContextVar("hinoki_event_muted", default=False)


class EventBus:
    """The handlers an application listed, in a fixed order, and ``emit``.

    Immutable after construction except for its counters. The runtime makes the
    application's bus current for each request, hook and command, the way it
    does the database.
    """

    def __init__(
        self,
        collections: Sequence[Handlers] = (),
        settings: EventsSettings | None = None,
    ) -> None:
        limits = settings or EventsSettings()
        self.handler_timeout = limits.handler_timeout
        self.max_handlers = limits.max_handlers
        self.max_depth = limits.max_depth
        self._by_kind: dict[type, list[Registration]] = {}
        self._count: Counter[tuple[EventKey, Phase]] = Counter()
        self._stats: Counter[tuple[str, str, str]] = Counter()
        # Subjects (None: every subject) with a handler per event class, and the
        # event keys with an in_transaction handler: O(1) guards for write paths.
        self._subjects: dict[type, set[type | None]] = {}
        self._transactional: set[EventKey] = set()
        # Native metrics (Kiso with metrics.listen): ``None`` costs one check.
        self._recorder: EventRecorder | None = None
        self._slots: dict[int, int] = {}
        self._load(collections)

    def _load(self, collections: Sequence[Handlers]) -> None:
        seen: set[int] = set()
        order = 0
        for collection in collections:
            if not isinstance(collection, Handlers):
                raise EventRegistrationError(
                    f"events= takes Handlers collections, not {collection!r}"
                )
            if id(collection) in seen:
                raise EventRegistrationError("the same Handlers is listed twice")
            seen.add(id(collection))
            for item in collection.registrations:
                self._add(dataclasses.replace(item, order=order))
                order += 1

    def _add(self, item: Registration) -> None:
        slot = (item.key, item.phase)
        self._count[slot] += 1
        if self._count[slot] > self.max_handlers:
            raise EventRegistrationError(
                f"{item.event_name} has more than {self.max_handlers} "
                f"{item.phase.value} handlers (events.max_handlers)"
            )
        self._by_kind.setdefault(item.key[0], []).append(item)
        self._subjects.setdefault(item.key[0], set()).add(item.key[1])
        if item.phase is Phase.IN_TRANSACTION:
            self._transactional.add(item.key)

    @property
    def registrations(self) -> tuple[Registration, ...]:
        """Every handler in execution order."""
        merged = [r for items in self._by_kind.values() for r in items]
        return tuple(sorted(merged, key=lambda r: r.order))

    def listening(self, kind: type) -> bool:
        """Whether emitting ``kind`` would do anything: a handler is registered
        for it or the calling context is capturing events. The cheap guard the
        write paths use before they build an event."""
        return kind in self._by_kind or _captured.get() is not None

    def registered(self, kind: type, subject: type | None) -> bool:
        """Whether a handler (any phase) is registered for ``kind`` with this
        subject: ``Kind[subject]`` or bare ``Kind``. Ignores ``capture()``."""
        subjects = self._subjects.get(kind)
        return subjects is not None and (None in subjects or subject in subjects)

    def wants(self, kind: type, subject: type | None) -> bool:
        """Whether a write should build and emit ``kind`` for ``subject``: a
        handler is registered for it or the context is capturing events."""
        return self.registered(kind, subject) or _captured.get() is not None

    def wants_transaction(self, kind: type, subject: type) -> bool:
        """Whether an ``in_transaction`` handler receives ``kind`` for
        ``subject``, so the write must run in a transaction that can roll back."""
        return (kind, None) in self._transactional or (
            kind,
            subject,
        ) in self._transactional

    def matching(self, event: object) -> list[Registration]:
        """The handlers that receive ``event``, in execution order. A handler
        registered for ``Event[Note]`` receives an event whose ``subject`` is
        ``Note``; one registered for ``Event`` receives every such event."""
        items = self._by_kind.get(type(event), ())
        subject = getattr(event, "subject", None)
        return [r for r in items if r.key[1] is None or r.key[1] is subject]

    async def notify(self, event: object) -> None:
        """Emit ``event`` for a decision that must not change because somebody
        listens (a refused request): no failure of a handler or of the event
        machinery propagates, and the whole delivery takes at most
        ``handler_timeout`` seconds."""
        try:
            async with asyncio.timeout(self.handler_timeout):
                await self.emit(event)
        except Exception:
            log.exception("delivering %s failed", type(event).__qualname__)

    async def emit(self, event: object) -> None:
        """Deliver ``event``: ``in_transaction`` handlers now, in order (the
        first failure stops the rest and propagates); ``after_commit`` handlers
        once the outermost transaction commits."""
        captured = _captured.get()
        if captured is not None:
            captured.append(event)
        handlers = self.matching(event)
        if not handlers or _muted.get():
            return
        depth = _depth.get()
        if depth >= self.max_depth:
            raise EventError(
                f"events nested deeper than {self.max_depth} "
                f"(events.max_depth) at {type(event).__qualname__}"
            )
        inside = [h for h in handlers if h.phase is Phase.IN_TRANSACTION]
        after = [h for h in handlers if h.phase is Phase.AFTER_COMMIT]
        token = _depth.set(depth + 1)
        try:
            if inside:
                self.require_transaction(event)
                for item in inside:
                    await self.run(item, event)
            if after:
                await self.defer(after, event, depth + 1)
        finally:
            _depth.reset(token)

    def require_transaction(self, event: object) -> None:
        if active_transaction() is None:
            raise EventError(
                f"{type(event).__qualname__} has in_transaction handlers but "
                "no transaction is open: emit it inside db.atomic() or a model write"
            )

    async def defer(
        self, handlers: list[Registration], event: object, depth: int
    ) -> None:
        async def deliver() -> None:
            token = _depth.set(depth)
            try:
                for item in handlers:
                    await self.run_isolated(item, event)
            finally:
                _depth.reset(token)

        database = current_database_or_none()
        if database is not None and database.active() is not None:
            database.after_commit(deliver)
        else:
            await deliver()

    async def run_isolated(self, item: Registration, event: object) -> None:
        try:
            await self.run(item, event)
        except Exception:
            log.exception("after_commit handler %s failed", item.name)

    async def run(self, item: Registration, event: object) -> None:
        outcome = "error"
        recorder = self._recorder
        started = 0.0 if recorder is None else perf_counter()
        try:
            await self.call(item, event)
            outcome = "ok"
        except EventTimeout:
            outcome = "timeout"
            raise
        finally:
            self._stats[(item.event_name, item.phase.value, outcome)] += 1
            if recorder is not None:
                recorder.record(
                    self._slots[item.order], OUTCOMES[outcome], perf_counter() - started
                )

    async def call(self, item: Registration, event: object) -> None:
        limit = asyncio.timeout(self.handler_timeout)
        try:
            async with limit:
                if item.is_async:
                    await cast("Awaitable[Any]", item.handler(event))
                else:
                    await self.call_sync(item, event)
        except TimeoutError as error:
            if not limit.expired():
                raise
            raise EventTimeout(
                f"event handler {item.name} timed out after "
                f"{self.handler_timeout:g}s (events.handler_timeout)"
            ) from error

    @staticmethod
    async def call_sync(item: Registration, event: object) -> None:
        runner = context.sync_runner.get()
        if runner is None:
            raise EventError(
                f"{item.name} is a plain 'def' handler and no worker executor "
                "is active; make it 'async def' or emit inside a request"
            )
        result = await runner(item.handler, event)
        if inspect.isawaitable(result):
            await result

    def stats(self) -> dict[tuple[str, str, str], int]:
        """Handler runs counted per event, phase and outcome (``ok``,
        ``error`` or ``timeout``) since the worker started."""
        return dict(self._stats)

    def metric_series(self) -> list[tuple[str, str]]:
        """The (event, phase) pairs of the registered handlers, in execution
        order and without repeats: the fixed label set of the event metrics."""
        pairs = ((r.event_name, r.phase.value) for r in self.registrations)
        return list(dict.fromkeys(pairs))

    def attach_metrics(self, recorder: EventRecorder | None) -> None:
        """Record every handler run, with its duration, into ``recorder`` (the
        native recorder of a Kiso server whose ``metrics.events`` series came
        from ``metric_series()``). ``None`` stops recording."""
        slots = {pair: n for n, pair in enumerate(self.metric_series())}
        self._slots = {
            r.order: slots[(r.event_name, r.phase.value)] for r in self.registrations
        }
        self._recorder = recorder

    @contextmanager
    def capture(self) -> Iterator[list[object]]:
        """Collect the events emitted in this block, whether or not a handler
        listens, for a test to assert on. Handlers still run."""
        seen: list[object] = []
        token = _captured.set(seen)
        try:
            yield seen
        finally:
            _captured.reset(token)

    @contextmanager
    def muted(self) -> Iterator[None]:
        """Deliver nothing in this block (``capture`` still records)."""
        token = _muted.set(True)
        try:
            yield
        finally:
            _muted.reset(token)


def current_database_or_none() -> Any:
    # Local import: ``hinoki.orm`` imports the model, which imports this module.
    from hinoki.orm.context import database_var  # noqa: PLC0415

    return database_var.get()


def active_transaction() -> ActiveTransaction | None:
    database = current_database_or_none()
    return None if database is None else database.active()


events_var: ContextVar[EventBus | None] = ContextVar("hinoki_events", default=None)


def current_events() -> EventBus:
    bus = events_var.get()
    if bus is None:
        raise EventError(
            "no event bus is active: serve an Hinoki application or use "
            "hinoki.events.use(bus)"
        )
    return bus


@contextmanager
def use(bus: EventBus) -> Iterator[EventBus]:
    """Make ``bus`` the current one inside the block (the runtime does this for
    every request, startup hook and management command)."""
    token = events_var.set(bus)
    try:
        yield bus
    finally:
        events_var.reset(token)


def listener(kind: type, subject: type | None = None) -> EventBus | None:
    """The current bus when a write of ``subject`` (or, for an event with no
    subject, any emitter) must emit ``kind``, else ``None``: the guard of the
    emitting code paths (one context-variable read when no application bus is
    current)."""
    bus = events_var.get()
    if bus is None or not bus.wants(kind, subject):
        return None
    return bus


async def emit(event: object) -> None:
    """Emit ``event`` on the current worker's bus."""
    await current_events().emit(event)


__all__ = [
    "AuthRejected",
    "Created",
    "Deleted",
    "EventBus",
    "EventError",
    "EventTimeout",
    "Handlers",
    "Phase",
    "RateLimited",
    "Registration",
    "Updated",
    "current_events",
    "emit",
    "events_var",
    "listener",
    "use",
]
