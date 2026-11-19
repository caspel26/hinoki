"""The native lane takes an async handler only with a static proof."""

import asyncio
import functools
from asyncio import current_task as current_task_alias
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from hinoki.orm import Model, field
from hinoki.runtime import nativeproof
from hinoki.runtime.nativeproof import native_refusal


class ProofNote(Model):
    id: int = field(primary_key=True)
    title: str


class OverriddenNote(Model):
    id: int = field(primary_key=True)

    @classmethod
    async def aget(cls, **conditions: Any) -> "OverriddenNote":
        await asyncio.sleep(0)
        return await super().aget(**conditions)


async def read_one(id: int) -> ProofNote:
    return await ProofNote.aget(id=id)


async def read_optional(id: int) -> ProofNote | None:
    return await ProofNote.aget_or_none(id=id)


async def read_first(title: str) -> ProofNote | None:
    return await ProofNote.filter(title=title).order_by("id").afirst()


async def read_counted(title: str) -> tuple[int, bool]:
    query = ProofNote.filter(title=title)
    return await query.acount(), await ProofNote.filter(title=title).aexists()


async def read_page() -> list[ProofNote]:
    return await ProofNote.filter(title="x").order_by("id").limit(5).offset(10)


async def read_all() -> list[ProofNote]:
    return await ProofNote.all()


async def no_await(id: int) -> dict[str, int]:
    return {"id": id}


@pytest.mark.parametrize(
    "handler",
    [read_one, read_optional, read_first, read_all, read_page, no_await],
)
def test_plain_native_reads_are_proved(handler: Callable[..., Any]) -> None:
    assert native_refusal(handler) is None


def test_reads_split_over_statements_are_refused_until_they_are_direct() -> None:
    # ``query`` is a local name: the proof does not follow assignments.
    assert native_refusal(read_counted) == "query is a local name"


async def sleeps() -> None:
    await asyncio.sleep(0)


async def gathers(id: int) -> Any:
    return await asyncio.gather(ProofNote.aget(id=id))


async def unknown_name(id: int) -> Any:
    return await helper(id)  # type: ignore[name-defined]  # noqa: F821


async def local_shadow() -> Any:
    ProofNote: Any = object()
    return await ProofNote.aget(id=1)


async def overridden() -> Any:
    return await OverriddenNote.aget(id=1)


async def writes(id: int) -> Any:
    return await ProofNote.aupdate(title="x")


async def chained_past_a_terminal(id: int) -> Any:
    return await ProofNote.aget(id=id).something()  # type: ignore[attr-defined]


async def async_with() -> None:
    async with asyncio.timeout(1):
        pass


async def async_for(items: Any) -> None:
    async for _ in items:
        pass


async def async_comprehension(items: Any) -> list[int]:
    return [x async for x in items]


async def nested_async() -> None:
    async def inner() -> None:
        return None

    await inner()


async def generates() -> Any:
    yield 1


async def awaits_inside_an_argument(id: int) -> Any:
    return await ProofNote.aget(id=await ProofNote.aget_or_none(id=id))


async def non_awaited_task() -> None:
    asyncio.create_task(sleeps())  # noqa: RUF006
    await ProofNote.aget(id=1)


@pytest.mark.parametrize(
    ("handler", "reason"),
    [
        (sleeps, "an await is not on a Model class"),
        (gathers, "an await is not on a Model class"),
        (unknown_name, "an await is not a call on a model"),
        (local_shadow, "ProofNote is a local name"),
        (overridden, "OverriddenNote.aget is overridden"),
        (writes, "ProofNote.aupdate is not a native query"),
        (chained_past_a_terminal, "aget is followed by more calls"),
        (async_with, "it uses AsyncWith"),
        (async_for, "it uses AsyncFor"),
        (async_comprehension, "it uses comprehension"),
        (nested_async, "it uses AsyncFunctionDef"),
        (generates, "not a plain async function"),
    ],
)
def test_anything_else_stays_on_the_python_path(
    handler: Callable[..., Any], reason: str
) -> None:
    assert native_refusal(handler) == reason


def test_an_unproven_await_nested_in_an_argument_is_found() -> None:
    assert native_refusal(awaits_inside_an_argument) is None


def test_a_task_started_without_await_is_refused() -> None:
    assert native_refusal(non_awaited_task) == "it uses asyncio directly"


def test_a_wrapped_handler_is_refused() -> None:
    def decorate(
        func: Callable[..., Awaitable[Any]],
    ) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await func(*args, **kwargs)

        return wrapper

    assert native_refusal(decorate(read_one)) == "wrapped by another decorator"


def test_things_that_are_not_plain_async_functions_are_refused() -> None:
    def sync() -> None:
        return None

    assert native_refusal(sync) == "not a plain async function"
    assert native_refusal(functools.partial(read_one, 1)) == (
        "not a plain async function"
    )


def test_a_function_without_source_is_refused() -> None:
    namespace: dict[str, Any] = {}
    # A unique filename: "<string>" collides with execnet's own source under xdist.
    exec(compile("async def made(): return 1", "<no-source>", "exec"), namespace)  # noqa: S102
    assert native_refusal(namespace["made"]) == "its source is not available"


def test_a_closure_name_is_resolved_to_its_model() -> None:
    model = ProofNote

    async def read() -> Any:
        return await model.aget(id=1)

    assert native_refusal(read) is None


def test_the_proof_exports_its_reason_type() -> None:
    assert issubclass(nativeproof.ProofError, Exception)


async def read_with_task_name(id: int) -> str:
    task = asyncio.current_task()
    assert task is not None
    return (await ProofNote.aget(id=id)).title + task.get_name()


def test_a_handler_using_the_current_task_is_refused() -> None:
    assert native_refusal(read_with_task_name) == "it uses asyncio directly"


async def imported_task_api() -> None:
    current_task_alias()
    await ProofNote.aget(id=1)


async def imports_task_api_inline() -> None:
    from asyncio import current_task as owner  # noqa: PLC0415

    owner()
    await ProofNote.aget(id=1)


def test_an_inline_import_of_a_task_api_is_refused() -> None:
    assert native_refusal(imports_task_api_inline) == "it uses asyncio directly"


def test_a_task_api_under_another_name_is_refused() -> None:
    assert native_refusal(imported_task_api) == "it uses asyncio directly"
