"""A limiter script run as a separate process by ``ratelimitsharedtest``: it hits one
database table ``HITS`` times and prints how many requests were admitted."""

import asyncio
import sys
from datetime import UTC, datetime

from hinoki import RateLimit, RateLimitContext
from hinoki.db import Database
from hinoki.ratelimit import DatabaseLimits
from hinoki.testing import ManualClock


async def main(url: str, limit: int, hits: int) -> int:
    database = await Database.connect(url, max_size=4)
    context = RateLimitContext(
        "GET",
        "/shared",
        "203.0.113.7",
        None,
        lambda kind: database if kind is Database else kind,
    )
    clock = ManualClock(datetime(2030, 1, 1, tzinfo=UTC))
    # SQLite has one writer; allow this deliberately concurrent process test the
    # documented maximum storage wait instead of the 0.5s production default.
    policy = RateLimit(limit, 3600, storage=DatabaseLimits(timeout=5), clock=clock)
    decisions = await asyncio.gather(*(policy.decide(context) for _ in range(hits)))
    database.close()
    return sum(decision.allowed for decision in decisions)


if __name__ == "__main__":
    print(asyncio.run(main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))))
