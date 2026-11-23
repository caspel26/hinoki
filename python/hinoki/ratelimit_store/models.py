"""The table behind ``hinoki.RateLimit`` with ``DatabaseLimits``. Its name is fixed,
so a class rename never moves it; the checked-in migration creates it."""

from hinoki import db
from hinoki.orm import Model, field


@db.indexed("state")
class RateLimitRow(Model, table="hinoki_rate_limits"):
    """One bucket: the SHA-256 digest of the route and client, and the limiter's
    own state. The limiter reads and writes it in native code, never through
    this class; it exists for the migration state. The index on ``state`` is what
    lets the sweep find expired buckets without reading the table."""

    bucket: str = field(primary_key=True, max_length=64)
    state: int = 0
