"""Calling async code from a sync handler.

Sync handlers run in the executor's threads. ``run_sync`` hands a coroutine to
the worker's event loop (where the database pool lives), keeping the caller's
context, and blocks that thread until it finishes. It refuses to run on the
event loop itself, where blocking would freeze the whole worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import types
import weakref
from collections.abc import Callable, Coroutine
from typing import Any

from hinoki import context

_CO_COROUTINE = inspect.CO_COROUTINE
_ASYNC_HOOK_CACHE: weakref.WeakKeyDictionary[object, bool] = weakref.WeakKeyDictionary()


def is_coroutine_hook(hook: Callable[..., Any]) -> bool:
    """Cached check for whether ``hook`` is a coroutine function."""
    if isinstance(hook, (types.FunctionType, types.MethodType)):
        if hook.__code__.co_flags & _CO_COROUTINE:
            return True
        target: object = hook.__func__ if isinstance(hook, types.MethodType) else hook
    else:
        target = hook
    try:
        cached = _ASYNC_HOOK_CACHE.get(target)
    except TypeError:
        return inspect.iscoroutinefunction(hook)
    if cached is not None:
        return cached
    result = inspect.iscoroutinefunction(hook)
    with contextlib.suppress(TypeError):
        _ASYNC_HOOK_CACHE[target] = result
    return result


def refuse_event_loop() -> None:
    """Raise when called on the event loop, where a sync model call would
    block the whole worker."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        "a sync model method was called on the event loop; await the "
        "a... variant instead"
    )


def run_sync[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Run ``coroutine`` on the worker's loop and wait for its result."""
    try:
        refuse_event_loop()
    except RuntimeError:
        coroutine.close()
        raise
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
    if is_coroutine_hook(hook):
        return await hook(*arguments)
    runner = context.sync_runner.get()
    if runner is None:
        raise RuntimeError("a sync hook can only run inside a request served by Hinoki")
    result = await runner(hook, *arguments)
    if inspect.isawaitable(result):  # a sync wrapper that returned a coroutine
        return await result
    return result
