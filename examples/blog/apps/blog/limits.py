"""A rate limit policy for ``POST /auth/login``.

Hinoki ships no limiter: a route names a policy and the application owns the rule,
the key and the storage. This one is a fixed window per client address, kept in
the memory of ONE worker, so it resets on restart and each worker counts on its
own. That is enough to show the seam; a production policy keeps its counters in a
store every worker shares, reached with ``context.resource(Store)`` from a module
that owns the connection (see the rate limiting section of the endpoints guide).
"""

import time
from collections.abc import Callable

from hinoki import RateLimitContext, errors

MAX_CLIENTS = 10_000  # bounded: a flood of addresses cannot grow the table forever


class LoginAttempts:
    """At most ``limit`` login requests per ``window`` seconds from one address."""

    def __init__(
        self,
        limit: int = 20,
        window: int = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window
        self.clock = clock
        self.windows: dict[str, tuple[float, int]] = {}

    def check(self, context: RateLimitContext) -> None:
        now = self.clock()
        key = context.client or "unknown"
        started, count = self.windows.get(key, (now, 0))
        if now - started >= self.window:
            started, count = now, 0
        if count >= self.limit:
            raise errors.TooManyRequests(max(1, int(started + self.window - now) + 1))
        if key not in self.windows and len(self.windows) >= MAX_CLIENTS:
            self.windows = {
                k: v for k, v in self.windows.items() if now - v[0] < self.window
            }
        self.windows[key] = (started, count + 1)
