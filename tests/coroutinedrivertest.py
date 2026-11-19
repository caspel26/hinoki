"""Rust drives a coroutine that awaits only native waits, with no event loop."""

import asyncio

import pytest

from hinoki import core


async def test_result_comes_back_through_the_waits() -> None:
    async def handler() -> int:
        first = await core._native_wait(1, 20, False)
        second = await core._native_wait(1, 22, False)
        return int(first) + int(second)

    assert core._drive(handler()) == 42


def test_a_failed_wait_raises_inside_the_handler() -> None:
    async def handler() -> str:
        try:
            await core._native_wait(1, None, True)
        except RuntimeError as error:
            return f"caught: {error}"
        return "unreachable"

    assert core._drive(handler()) == "caught: native wait failed"


def test_a_non_native_await_is_refused_and_runs_finally() -> None:
    cleaned: list[str] = []

    async def handler() -> None:
        try:
            await asyncio.sleep(0)
            await core._native_wait(1, None, False)
        finally:
            cleaned.append("finally")

    with pytest.raises(RuntimeError, match="not a native awaitable"):
        core._drive(handler())
    assert cleaned == ["finally"]


def test_a_future_is_not_mistaken_for_a_native_wait() -> None:
    loop = asyncio.new_event_loop()
    try:
        future = loop.create_future()

        async def handler() -> None:
            await future

        with pytest.raises(RuntimeError, match="not a native awaitable"):
            core._drive(handler())
    finally:
        loop.close()


def test_cancellation_runs_finally_and_reaches_the_caller() -> None:
    cleaned: list[str] = []

    async def handler() -> None:
        try:
            await core._native_wait(1, None, False)
            await core._native_wait(1, None, False)
        finally:
            cleaned.append("finally")

    with pytest.raises(asyncio.CancelledError):
        core._drive(handler(), cancel_after=1)
    assert cleaned == ["finally"]


def test_a_handler_may_catch_the_cancellation_and_finish() -> None:
    async def handler() -> str:
        try:
            await core._native_wait(1, None, False)
        except asyncio.CancelledError:
            return "cleaned up"
        return "done"

    assert core._drive(handler(), cancel_after=0) == "cleaned up"


def test_a_handler_without_awaits_returns_directly() -> None:
    async def handler() -> str:
        return "plain"

    assert core._drive(handler()) == "plain"


def test_the_handler_exception_propagates() -> None:
    async def handler() -> None:
        await core._native_wait(1, None, False)
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        core._drive(handler())
