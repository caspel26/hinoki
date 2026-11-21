"""Run a request coroutine without a timer until it first suspends.

A handler chain that never waits (no I/O, no ``await`` of a pending future)
finishes in its first step. Nothing can interrupt such a step, so a deadline
timer for it is pure cost. ``run_inline`` takes that first step by hand and only
when the coroutine suspends does it hand the coroutine to the running task
under ``asyncio.timeout``, with what is left of the deadline. It is fail-safe by
construction: there is no static guess about the handler, only what it actually
did.
"""

from __future__ import annotations

import asyncio
import time
import types
from collections.abc import Coroutine, Generator
from typing import Any


class RequestDeadline(Exception):
    """The request ran past its deadline and was cancelled."""


@types.coroutine
def continue_coroutine(
    coro: Coroutine[Any, Any, Any], yielded: Any
) -> Generator[Any, Any, Any]:
    """Await ``coro`` from the task that now owns it, after it yielded
    ``yielded`` to the event loop: forward what the loop sends or throws."""
    while True:
        try:
            sent = yield yielded
        except GeneratorExit:
            coro.close()
            raise
        except BaseException as exc:
            step = coro.throw
            argument: Any = exc
        else:
            step = coro.send
            argument = sent
        try:
            yielded = step(argument)
        except StopIteration as stop:
            return stop.value


async def run_inline(coro: Coroutine[Any, Any, Any], timeout: float) -> Any:
    """The result of ``coro``; it raises ``RequestDeadline`` when ``timeout``
    seconds (counted from this call) pass before it finishes."""
    started = time.monotonic()
    try:
        yielded = coro.send(None)
    except StopIteration as stop:
        return stop.value
    limit = asyncio.timeout(max(timeout - (time.monotonic() - started), 0.0))
    try:
        async with limit:
            return await continue_coroutine(coro, yielded)
    except TimeoutError as exc:
        if limit.expired():
            raise RequestDeadline from exc
        raise
