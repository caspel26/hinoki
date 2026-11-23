"""The table of the shared rate limiter.

Optional and explicit. Register it as an app and apply its migration
(``hinoki app add ratelimit --models hinoki.ratelimit_store.models --migrations
hinoki.ratelimit_store.migrations``, then ``hinoki migrations apply ratelimit``);
importing this package never touches a database.
"""

from hinoki.ratelimit_store.models import RateLimitRow

__all__ = ["RateLimitRow"]
