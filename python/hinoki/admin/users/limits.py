"""The default policy on the admin's sign-in and password routes.

A fixed window per client address, in the memory of ONE worker: it resets on
restart and each worker counts on its own, so it only slows a guesser by the number
of workers. Hash cost (Argon2id) is the main brake; a deployment that needs a
shared limit passes its own policy, see the rate limit section of the endpoints guide.
"""

from __future__ import annotations

import hashlib
import ipaddress
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta

from hinoki.admin.users.models import LoginWindowRow
from hinoki.db import UniqueViolation
from hinoki.errors import TooManyRequests
from hinoki.ratelimit import RateLimitContext
from hinoki.utils import utc_now

MAX_CLIENTS = 10_000  # the table never holds more addresses than this
UNKNOWN_FACTOR = 10  # clients with no known address share one, larger, window


def window_key(client: str | None) -> str:
    """One window per address; an IPv6 address counts by its /64, because a
    single host controls a whole /64 of them."""
    if client is None:
        return "unknown"
    try:
        address = ipaddress.ip_address(client)
    except ValueError:
        return client
    if isinstance(address, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


class LoginWindow:
    """At most ``limit`` attempts per ``window`` seconds from one address.

    Requests with no known client address (a Unix socket, a proxy that is not
    listed in ``server.trusted_proxies``) share one window ``UNKNOWN_FACTOR``
    times as large, so they cannot lock everyone out of sign-in with a few
    requests."""

    def __init__(
        self,
        limit: int = 10,
        window: int = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window
        self.clock = clock
        # Ordered by when each window started, oldest first, so expired windows are
        # dropped from the front and a full table evicts its oldest address.
        self.windows: OrderedDict[str, tuple[float, int]] = OrderedDict()

    def check(self, context: RateLimitContext) -> None:
        now = self.clock()
        key = window_key(context.client)
        limit = self.limit * (UNKNOWN_FACTOR if context.client is None else 1)
        self.expire(now)
        started, count = self.windows.get(key, (now, 0))
        if count >= limit:
            raise TooManyRequests(max(1, int(started + self.window - now) + 1))
        if key not in self.windows and len(self.windows) >= MAX_CLIENTS:
            self.windows.popitem(last=False)
        if key not in self.windows or started == now:
            self.windows.pop(key, None)  # a new window goes to the back
        self.windows[key] = (started, count + 1)

    def expire(self, now: float) -> None:
        while self.windows:
            oldest = next(iter(self.windows.values()))
            if now - oldest[0] < self.window:
                break
            self.windows.popitem(last=False)


class DatabaseLoginWindow:
    """The same window as ``LoginWindow``, kept in the ``hinoki_admin_login_windows``
    table so every worker and every host counts together and a restart keeps it.

    Clients are stored as a SHA-256 digest, never as an address. A count moves
    with a conditional update (compare and swap), so concurrent attempts never
    pass more than ``limit``; when the swap keeps losing, the attempt is refused."""

    ATTEMPTS = 8

    def __init__(
        self,
        limit: int = 10,
        window: int = 60,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.limit = limit
        self.window = window
        self.clock = clock

    async def check(self, context: RateLimitContext) -> None:
        now = self.clock()
        key = hashlib.sha256(window_key(context.client).encode()).hexdigest()
        limit = self.limit * (UNKNOWN_FACTOR if context.client is None else 1)
        await LoginWindowRow.adelete(
            started_at__lt=now - timedelta(seconds=self.window)
        )
        for _ in range(self.ATTEMPTS):
            if await self.count(key, now, limit):
                return
        raise TooManyRequests(1)

    async def count(self, key: str, now: datetime, limit: int) -> bool:
        """Count one attempt; False when another worker won the race."""
        row = await LoginWindowRow.aget_or_none(key=key)
        if row is None:
            try:
                await LoginWindowRow.acreate(
                    key=key, started_at=now, count=1, returning=False
                )
            except UniqueViolation:
                return False
            return True
        if row.count >= limit:
            left = row.started_at + timedelta(seconds=self.window) - now
            raise TooManyRequests(max(1, int(left.total_seconds()) + 1))
        won = await LoginWindowRow.aupdate_where(
            {"key": key, "count": row.count}, {"count": row.count + 1}, returning=False
        )
        return bool(won)
