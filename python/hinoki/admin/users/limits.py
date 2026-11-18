"""The default policy on the admin's sign-in route.

A fixed window per client address, in the memory of ONE worker: it resets on
restart and each worker counts on its own, so it only slows a guesser by the number
of workers. Hash cost (Argon2id) is the main brake; a deployment that needs a
shared limit passes its own policy, see the rate limit section of the endpoints guide.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from hinoki.errors import TooManyRequests
from hinoki.ratelimit import RateLimitContext

MAX_CLIENTS = 10_000  # bounded: a flood of addresses cannot grow the table forever


class LoginWindow:
    """At most ``limit`` sign-in attempts per ``window`` seconds from one address."""

    def __init__(
        self,
        limit: int = 10,
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
            raise TooManyRequests(max(1, int(started + self.window - now) + 1))
        if key not in self.windows and len(self.windows) >= MAX_CLIENTS:
            self.windows = {
                k: v for k, v in self.windows.items() if now - v[0] < self.window
            }
        self.windows[key] = (started, count + 1)
