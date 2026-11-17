"""Sync/async bridge behavior outside and inside a managed request context."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from hinoki import context
from hinoki.blocking import call_hook, run_sync


@pytest.mark.asyncio
async def test_run_sync_rejects_event_loop_and_missing_worker_context() -> None:
    async def value() -> int:
        return 7

    with pytest.raises(RuntimeError, match="called on the event loop"):
        run_sync(value())

    with pytest.raises(RuntimeError, match="only work inside sync handlers"):
        await asyncio.to_thread(run_sync, value())


@pytest.mark.asyncio
async def test_run_sync_hands_work_to_worker_loop_without_handoff() -> None:
    async def value() -> int:
        await asyncio.sleep(0)
        return 11

    loop_token = context.event_loop.set(asyncio.get_running_loop())
    try:
        result = await asyncio.to_thread(run_sync, value())
    finally:
        context.event_loop.reset(loop_token)
    assert result == 11


@pytest.mark.asyncio
async def test_run_sync_refuses_handoff_that_closes_during_registration() -> None:
    finished = asyncio.Event()
    started = threading.Event()

    async def value() -> int:
        started.set()
        try:
            await asyncio.Event().wait()
            return 0
        finally:
            finished.set()

    class ClosingHandoff(context.Handoff):
        def add(self, future: Any) -> bool:
            assert started.wait(timeout=2)
            self.close()
            return super().add(future)

    loop_token = context.event_loop.set(asyncio.get_running_loop())
    handoff_token = context.handed_off.set(ClosingHandoff())
    try:
        with pytest.raises(RuntimeError, match="request ended"):
            await asyncio.to_thread(run_sync, value())
        await asyncio.wait_for(finished.wait(), timeout=2)
    finally:
        context.handed_off.reset(handoff_token)
        context.event_loop.reset(loop_token)


@pytest.mark.asyncio
async def test_call_hook_requires_runner_and_awaits_coroutine_from_sync_hook() -> None:
    def sync_hook(value: int) -> Any:
        async def result() -> int:
            return value * 2

        return result()

    with pytest.raises(RuntimeError, match="only run inside a request"):
        await call_hook(sync_hook, 3)

    async def runner(hook: Any, *args: Any) -> Any:
        return hook(*args)

    token = context.sync_runner.set(runner)
    try:
        assert await call_hook(sync_hook, 3) == 6
    finally:
        context.sync_runner.reset(token)


@pytest.mark.asyncio
async def test_call_hook_awaits_async_hooks_and_returns_plain_sync_values() -> None:
    async def async_hook(value: int) -> int:
        return value + 1

    assert await call_hook(async_hook, 4) == 5

    token = context.sync_runner.set(
        lambda hook, *args: asyncio.sleep(0, result=hook(*args))
    )
    try:
        assert await call_hook(lambda value: value * 3, 4) == 12
    finally:
        context.sync_runner.reset(token)
