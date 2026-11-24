"""The rate-limit seam: a route asks the application whether a request may proceed.

A route names the policy that decides:

    @http.post("/auth/login", rate_limit=LoginLimit())
    async def login(data: LoginIn) -> SessionOut: ...

The policy runs after the route is matched and the caller authenticated, before the
body is read or the handler runs. It returns to allow the request and raises
``errors.TooManyRequests(retry_after)`` to refuse it.

``RateLimit`` is the limiter Hinoki ships: a generic cell rate algorithm kept in a
database table shared by every worker and host (or, for development, in the memory
of one worker). The counting runs in the native core, one SQL statement per request.
Any other object with a ``check`` method is a policy too, and owns its quota, key
and storage.
"""

from __future__ import annotations

import inspect
import logging
import math
import re
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

from hinoki import core
from hinoki.db import Database
from hinoki.errors import TooManyRequests

if TYPE_CHECKING:
    from hinoki.auth import Caller

T = TypeVar("T")

log = logging.getLogger("hinoki.ratelimit")

MAX_WINDOW = 86_400
MIN_TIMEOUT = 0.001
MAX_TIMEOUT = 5.0
MAX_U32 = 2**32 - 1
MAX_ROWS = 2**63 - 1
MICROS = 1_000_000
MAX_SEGMENT = 1024
WARN_EVERY = 60.0  # seconds between two fail-open warnings
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
TABLE = re.compile(r"[a-z_][a-z0-9_]{0,62}")
CLIENT, CALLER, CUSTOM = 0, 1, 2

_memory_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class RateLimitContext:
    """What a policy may decide from: no headers, credentials, body or query."""

    method: str
    route: str  # the path template, for example ``/tasks/{task_id}``
    client: str | None  # the trusted client address, ``None`` when unknown
    caller: Caller[Any] | None  # the authenticated caller, ``None`` for a public route
    lookup: Callable[[type[Any]], Any] = field(repr=False)

    def resource(self, kind: type[T]) -> T:
        """The worker-owned resource of this type that a module provided (for
        example a Redis client or the ``Database``); ``LookupError`` if none."""
        found: T = self.lookup(kind)
        return found


class RateLimitPolicy(Protocol):
    """Anything with a ``check`` method, ``def`` or ``async def``.

    A sync ``check`` runs on the bounded executor and must use sync clients with
    finite timeouts; an async one runs on the event loop."""

    def check(self, context: RateLimitContext) -> Awaitable[None] | None: ...


KeySource = Literal["client", "caller"] | Callable[[RateLimitContext], str]
Tenant = Callable[[RateLimitContext], str | None]


@dataclass(frozen=True, slots=True)
class DatabaseLimits:
    """Buckets in a table of the worker's ``Database``, shared by every worker.

    ``max_rows`` caps the table: once full, new keys share one overflow bucket
    per scope, ``unknown_factor`` times as large, until expired rows are swept.
    ``timeout`` (seconds, at most 5) bounds one check, the wait for a connection
    included."""

    table: str = "hinoki_rate_limits"
    max_rows: int = 1_000_000
    timeout: float = 0.5
    sweep_every: int = 1000


@dataclass(frozen=True, slots=True)
class MemoryLimits:
    """Buckets in the memory of ONE worker: not shared, reset on restart. For
    development and tests. The buckets belong to this instance, created at the
    first request, so routes given the same instance and the same ``scope`` and
    ``tenant`` count together."""

    max_rows: int = 100_000
    sweep_every: int = 1000
    _cell: list[core.RateLimiter] = field(
        default_factory=list, init=False, repr=False, compare=False
    )

    def limiter(self) -> core.RateLimiter:
        with _memory_lock:
            if not self._cell:
                self._cell.append(
                    core.RateLimiter.memory(self.max_rows, self.sweep_every)
                )
            return self._cell[0]


Storage = DatabaseLimits | MemoryLimits


def _whole(value: object, name: str, low: int, high: int | None = None) -> int:
    if type(value) is not int or value < low or (high is not None and value > high):
        bound = f" and at most {high}" if high is not None else ""
        raise ValueError(f"{name} must be a whole number of at least {low}{bound}")
    return value


def _check_storage(storage: Storage) -> None:
    _whole(storage.max_rows, "max_rows", 1, MAX_ROWS)
    _whole(storage.sweep_every, "sweep_every", 1, MAX_U32)
    if isinstance(storage, DatabaseLimits):
        if not TABLE.fullmatch(storage.table):
            raise ValueError("table must be a lowercase identifier of at most 63 chars")
        timeout = storage.timeout
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not MIN_TIMEOUT <= timeout <= MAX_TIMEOUT
        ):
            raise ValueError("timeout must be from 0.001 to 5 seconds")


def _check_callable(value: object, name: str) -> None:
    if not callable(value) or inspect.iscoroutinefunction(value):
        raise ValueError(f"{name} must be a plain (not async) callable")


def _window_seconds(window: float) -> int:
    if (
        isinstance(window, bool)
        or not isinstance(window, int | float)
        or not math.isfinite(window)
        or not window.is_integer()
        or not 1 <= window <= MAX_WINDOW
    ):
        raise ValueError(f"window must be whole seconds from 1 to {MAX_WINDOW}")
    return int(window)


def _microseconds(moment: datetime) -> int:
    if moment.tzinfo is None:
        raise ValueError("the clock must return an aware datetime")
    return (moment - EPOCH) // timedelta(microseconds=1)


class _Warner:
    """At most one message per interval and per process, whatever the traffic."""

    def __init__(self) -> None:
        self.last = -WARN_EVERY
        self.lock = threading.Lock()

    def due(self) -> bool:
        now = time.monotonic()
        with self.lock:
            if now - self.last < WARN_EVERY:
                return False
            self.last = now
            return True


_fail_open_warner = _Warner()
_missing_table_warner = _Warner()


class RateLimit:
    """At most ``limit`` requests per ``window`` seconds for one key, counted in
    one shared table (or in memory, see ``storage``).

    A cell rate algorithm: a burst of ``limit`` is admitted at once and the
    sustained rate is ``limit`` per ``window``. A refusal does not use up quota.
    The key is hashed in the native core, so a client address, user id or custom
    key never reaches SQL, logs or metrics.

    ``key`` is ``"client"`` (the trusted address, an IPv6 one by its /64),
    ``"caller"`` (the authenticated caller, the client for an anonymous request)
    or a plain function of the context returning a string. ``scope`` defaults to the
    method and the route, so routes count apart; give routes the same ``scope`` to
    share a quota.
    ``tenant`` adds a segment to the key. A request with no known client shares one
    bucket ``unknown_factor`` times as large. ``on_error="closed"`` answers 503 when
    storage is unavailable, ``"open"`` admits the request and counts it.

    ``headers=True`` adds ``RateLimit-Limit``, ``RateLimit-Remaining`` and
    ``RateLimit-Reset`` (whole seconds) to admitted and refused answers.
    ``clock`` returns an aware ``datetime`` (``hinoki.testing.ManualClock``); the
    default is the system clock."""

    def __init__(  # noqa: PLR0913 - the limiter's keyword-only options
        self,
        limit: int,
        window: float,
        *,
        key: KeySource = "client",
        tenant: Tenant | None = None,
        cost: int = 1,
        scope: str | None = None,
        unknown_factor: int = 10,
        headers: bool = False,
        on_error: Literal["closed", "open"] = "closed",
        storage: Storage | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        _whole(limit, "limit", 1, MAX_U32)
        _whole(cost, "cost", 1, limit)
        _whole(unknown_factor, "unknown_factor", 1, MAX_U32)
        if key not in ("client", "caller"):
            _check_callable(key, "key")
        if tenant is not None:
            _check_callable(tenant, "tenant")
        if clock is not None:
            _check_callable(clock, "clock")
        if on_error not in ("closed", "open"):
            raise ValueError('on_error must be "closed" or "open"')
        if scope is not None and not 0 < len(scope) <= MAX_SEGMENT:
            raise ValueError("scope must be 1 to 1024 characters")
        self.storage: Storage = storage or DatabaseLimits()
        _check_storage(self.storage)
        self.limit = limit
        self.window = _window_seconds(window)
        if limit > self.window * MICROS:
            raise ValueError("limit must be at most one request per microsecond")
        self.key = key
        self.tenant = tenant
        self.scope = scope
        self.headers = headers
        self.on_error = on_error
        self.clock = clock
        self.rule = core.RateRule(
            limit, self.window, cost, unknown_factor, on_error == "open"
        )
        self._limiters: dict[int, tuple[object, core.RateLimiter]] = {}
        self._lock = threading.Lock()
        self._metrics: core.RateLimitMetrics | None = None

    def attach_metrics(self, metrics: core.RateLimitMetrics) -> None:
        """Count this limiter's decisions into the server's metrics, replacing
        the metrics of an earlier worker in this process."""
        with self._lock:
            self._metrics = metrics
            for _, limiter in self._limiters.values():
                limiter.attach_metrics(metrics)

    def release(self) -> None:
        """Forget the limiters bound to a worker's database, and its metrics, when
        that worker shuts down; a later worker binds its own. Memory buckets stay
        with their ``MemoryLimits``."""
        with self._lock:
            self._limiters.clear()
            self._metrics = None

    def _limiter(self, context: RateLimitContext) -> core.RateLimiter:
        storage = self.storage
        if isinstance(storage, MemoryLimits):
            return self._bind(0, None, storage.limiter)
        engine = context.resource(Database).engine
        return self._bind(id(engine), engine, lambda: self._database(engine, storage))

    def _bind(
        self, ident: int, owner: object, make: Callable[[], core.RateLimiter]
    ) -> core.RateLimiter:
        with self._lock:
            found = self._limiters.get(ident)
            if found is not None:
                return found[1]
            limiter = make()
            if self._metrics is not None:
                limiter.attach_metrics(self._metrics)
            # The strong reference to the engine keeps its id from being reused.
            self._limiters[ident] = (owner, limiter)
            return limiter

    @staticmethod
    def _database(engine: core.Database, storage: DatabaseLimits) -> core.RateLimiter:
        return core.RateLimiter.database(
            engine,
            storage.table,
            storage.max_rows,
            round(storage.timeout * 1000),
            storage.sweep_every,
        )

    def _target(self, context: RateLimitContext) -> tuple[int, str | None]:
        key = self.key
        if key == "client":
            return CLIENT, context.client
        if key == "caller":
            caller = context.caller
            if caller is None:
                return CLIENT, context.client
            return CALLER, str(caller.id)
        return CUSTOM, str(key(context))

    def _now(self) -> int | None:
        return None if self.clock is None else _microseconds(self.clock())

    def _arguments(
        self, context: RateLimitContext
    ) -> tuple[str, tuple[int, str | None], str | None, int | None]:
        tenant = None if self.tenant is None else self.tenant(context)
        scope = self.scope or f"{context.method} {context.route}"
        return scope, self._target(context), tenant, self._now()

    def _note(self, decision: core.RateDecision) -> core.RateDecision:
        if decision.failed_open and _fail_open_warner.due():
            log.warning("rate limit storage unavailable: requests are admitted")
        return decision

    @staticmethod
    def _unavailable(error: core.RateLimitUnavailable) -> None:
        if "table" in str(error) and _missing_table_warner.due():
            log.error(
                "the rate limit table is missing or out of date: "
                "run `hinoki migrations apply` for the app that owns it"
            )

    async def decide(self, context: RateLimitContext) -> core.RateDecision:
        """Count one hit and return the decision; raises
        ``core.RateLimitUnavailable`` when storage fails and ``on_error`` is closed."""
        limiter = self._limiter(context)
        scope, target, tenant, now = self._arguments(context)
        try:
            decision = await limiter.check(self.rule, scope, target, tenant, now)
        except core.RateLimitUnavailable as error:
            self._unavailable(error)
            raise
        return self._note(decision)

    def decide_sync(self, context: RateLimitContext) -> core.RateDecision:
        """``decide`` for a thread with no event loop. Raises ``RuntimeError`` on
        an event loop thread."""
        limiter = self._limiter(context)
        scope, target, tenant, now = self._arguments(context)
        try:
            decision = limiter.check_blocking(self.rule, scope, target, tenant, now)
        except core.RateLimitUnavailable as error:
            self._unavailable(error)
            raise
        return self._note(decision)

    def header_items(self, decision: core.RateDecision) -> tuple[tuple[str, str], ...]:
        """The ``RateLimit-*`` headers of a decision, or none unless ``headers``."""
        if not self.headers:
            return ()
        return (
            ("ratelimit-limit", str(decision.limit)),
            ("ratelimit-remaining", str(decision.remaining)),
            ("ratelimit-reset", str(decision.reset)),
        )

    def refusal(self, decision: core.RateDecision) -> TooManyRequests:
        """The ``TooManyRequests`` of a refused decision."""
        error = TooManyRequests(max(1, decision.retry_after))
        error.headers.update(self.header_items(decision))
        return error

    async def check(self, context: RateLimitContext) -> None:
        decision = await self.decide(context)
        if not decision.allowed:
            raise self.refusal(decision)

    def check_sync(self, context: RateLimitContext) -> None:
        decision = self.decide_sync(context)
        if not decision.allowed:
            raise self.refusal(decision)


__all__ = [
    "DatabaseLimits",
    "MemoryLimits",
    "RateLimit",
    "RateLimitContext",
    "RateLimitPolicy",
]
