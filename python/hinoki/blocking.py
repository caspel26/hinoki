"""Calling async code from a sync handler.

Sync handlers run in the executor's threads. ``run_sync`` hands a coroutine to
the worker's event loop (where the database pool lives), keeping the caller's
context, and blocks that thread until it finishes. It refuses to run on the
event loop itself, where blocking would freeze the whole worker.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Coroutine
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
    handoff = context.handed_off.get()
    if handoff is not None and handoff.closed:
        coroutine.close()
        raise RuntimeError("the request ended; sync model calls are refused")
    future = asyncio.run_coroutine_threadsafe(coroutine, loop)
    if handoff is None:
        return future.result()
    if not handoff.add(future):
        future.cancel()
        raise RuntimeError("the request ended; sync model calls are refused")
    try:
        return future.result()
    finally:
        handoff.discard(future)


async def call_hook(hook: Callable[..., Any], *arguments: Any) -> Any:
    """Call a framework hook that may be written ``async def`` or plain ``def``.

    Async hooks are awaited on the event loop. A plain ``def`` hook runs on the
    bounded sync executor (so blocking work and the sync model methods are safe
    in it) and counts toward the same queue limit as sync handlers."""
    if inspect.iscoroutinefunction(hook):
        return await hook(*arguments)
    runner = context.sync_runner.get()
    if runner is None:
        raise RuntimeError("a sync hook can only run inside a request served by Hinoki")
    result = await runner(hook, *arguments)
    if inspect.isawaitable(result):  # a sync wrapper that returned a coroutine
        return await result
    return result
