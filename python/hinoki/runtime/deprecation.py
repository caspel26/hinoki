"""What a deprecated action tells its callers: a ``Deprecation`` response header and
a bounded warning log."""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hinoki.routing import Route

log = logging.getLogger("hinoki.deprecated")

# Seconds between two warnings about the same action.
REPORT_EVERY = 60.0


def header_of(deprecated: bool | date) -> tuple[str, str] | None:
    """The ``Deprecation`` header of an action: RFC 9745's date form when the
    action names the date, else ``true``."""
    if deprecated is False:
        return None
    if deprecated is True:
        return ("deprecation", "true")
    moment = datetime(deprecated.year, deprecated.month, deprecated.day, tzinfo=UTC)
    return ("deprecation", f"@{int(moment.timestamp())}")


class Reminders:
    """Warns that a deprecated action was called, at most once per
    ``REPORT_EVERY`` seconds for each action, with the calls it stayed quiet about.
    One entry per deprecated action bounds the memory."""

    def __init__(self, routes: Sequence[Route]) -> None:
        self.state: dict[int, list[float | int]] = {
            id(route): [float("-inf"), 0] for route in routes if route.deprecated
        }

    def called(self, route: Route, now: float | None = None) -> None:
        entry = self.state.get(id(route))
        if entry is None:
            return
        moment = time.monotonic() if now is None else now
        entry[1] = int(entry[1]) + 1
        if moment - float(entry[0]) < REPORT_EVERY:
            return
        log.warning(
            "deprecated action called: %s %s (%d call(s) since the last report)",
            route.method,
            route.path,
            entry[1],
        )
        entry[0], entry[1] = moment, 0
