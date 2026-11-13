"""Calling async code from a sync handler.

Sync handlers run in the executor's threads. ``run_sync`` hands a coroutine to
the worker's event loop (where the database pool lives), keeping the caller's
context, and blocks that thread until it finishes. It refuses to run on the
event loop itself, where blocking would freeze the whole worker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from hinoki import context


def run_sync[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Run ``coroutine`` on the worker's loop and wait for its result."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        coroutine.close()
        raise RuntimeError(
            "a sync model method was called on the event loop; await the "
            "a... variant instead"
        )
    loop = context.event_loop.get()
    if loop is None:
        coroutine.close()
        raise RuntimeError(
            "sync model methods only work inside sync handlers served by Hinoki"
        )
    future = asyncio.run_coroutine_threadsafe(coroutine, loop)
    handed = context.handed_off.get()
    if handed is not None:
        handed.append(future)
    return future.result()
