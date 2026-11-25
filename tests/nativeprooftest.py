"""The native lane takes an async handler only with a static proof."""

import asyncio
import functools
import linecache
import sys
from asyncio import current_task as current_task_alias
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from hinoki.orm import Model, OnDelete, field, foreign_key
from hinoki.runtime import nativeproof
from hinoki.runtime.nativeproof import native_refusal
from hinoki.runtime.pipeline import announce_rate_limited as _foreign_helper


class ProofNote(Model):
    id: int = field(primary_key=True)
    title: str


class HookedNote(Model):
    id: int = field(primary_key=True)
    title: str

    async def after_save(self) -> None:
        await asyncio.sleep(0)


class ReplacedNote(Model):
    id: int = field(primary_key=True)
    title: str

    async def write_insert(self, returning: bool) -> None:
        await asyncio.sleep(0)
        await super().write_insert(returning)


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


class ProofWriter(Model):
    id: int = field(primary_key=True)


class ProofPiece(Model):
    id: int = field(primary_key=True)
    writer_id: int = foreign_key(
        ProofWriter, on_delete=OnDelete.CASCADE, related_name="pieces"
    )


async def read_joined(id: int) -> ProofPiece:
    return await ProofPiece.filter(id=id).select_related("writer").aget()


async def read_joined_rows() -> list[ProofPiece]:
    return await ProofPiece.all().select_related("writer").order_by("id").limit(5)


@pytest.mark.parametrize(
    "handler",
    [
        read_one,
        read_optional,
        read_first,
        read_all,
        read_page,
        no_await,
        read_joined,
        read_joined_rows,
    ],
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
    await ProofNote.acreate(title="x")
    await ProofNote.aupdate(id=id, title="x")
    await ProofNote.filter(id=id).aupdate({"title": "y"})
    return await ProofNote.adelete(id=id)


async def hooked_write() -> Any:
    return await HookedNote.acreate(title="x")


async def hooked_queryset_write() -> Any:
    return await HookedNote.filter(id=1).adelete()


async def replaced_write_path() -> Any:
    return await ReplacedNote.acreate(title="x")


async def unknown_write() -> Any:
    return await ProofNote.filter(id=1).aupdate_from()  # type: ignore[attr-defined]


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


async def prefetches() -> Any:
    return await ProofWriter.all().prefetch_related("pieces").afirst()


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
        (hooked_write, "HookedNote has write hooks"),
        (hooked_queryset_write, "HookedNote has write hooks"),
        (replaced_write_path, "ReplacedNote.write_insert is overridden"),
        (unknown_write, "aupdate_from is not a native query step"),
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


def test_stock_writes_on_a_hookless_model_are_native() -> None:
    assert native_refusal(writes) is None
    assert native_refusal(prefetches) is None


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


_this = sys.modules[__name__]


async def reads_through_a_module() -> Any:
    return await _this.ProofNote.aget(id=1)


async def reads_a_name_the_module_lacks() -> Any:
    return await _this.NotYetDefined.aget(id=1)


async def reads_through_a_class_attribute() -> Any:
    return await ProofNote.manager.aget(id=1)  # type: ignore[attr-defined]


async def starts_from_a_call() -> Any:
    return await sleeps()().aget(id=1)  # type: ignore[operator]


async def unknown_model_method() -> Any:
    return await ProofNote.not_a_query(id=1)  # type: ignore[attr-defined]


async def imports_asyncio_inline() -> None:
    import asyncio as aio  # noqa: PLC0415

    del aio
    await ProofNote.aget(id=1)


@pytest.mark.parametrize(
    ("handler", "reason"),
    [
        (reads_through_a_module, None),
        (reads_a_name_the_module_lacks, "NotYetDefined is not defined yet"),
        (
            reads_through_a_class_attribute,
            "manager is read from something that is not a module",
        ),
        (starts_from_a_call, "an await starts from something other than a name"),
        (unknown_model_method, "ProofNote.not_a_query is not a native query"),
        (imports_asyncio_inline, "it uses asyncio directly"),
    ],
)
def test_names_and_imports_the_proof_cannot_follow_are_refused(
    handler: Callable[..., Any], reason: str | None
) -> None:
    assert native_refusal(handler) == reason


def test_a_closure_name_that_is_not_bound_yet_is_refused() -> None:
    later: type[ProofNote]

    async def read() -> Any:
        return await later.aget(id=1)

    assert native_refusal(read) == "later is not bound yet"
    later = ProofNote
    assert native_refusal(read) is None


def test_a_source_that_is_not_one_async_function_is_refused() -> None:
    filename = "<not-a-function>"
    lines = ["x = 1\n"]
    linecache.cache[filename] = (len("".join(lines)), None, lines, filename)
    namespace: dict[str, Any] = {}
    try:
        exec(compile("async def made(): return 1", filename, "exec"), namespace)  # noqa: S102
        assert native_refusal(namespace["made"]) == (
            "its source is not one async function"
        )
    finally:
        linecache.cache.pop(filename, None)


async def _local_page_helper(title: str) -> list[ProofNote]:
    return await ProofNote.filter(title=title).order_by("id").limit(5)


async def _nested_helper(title: str) -> list[ProofNote]:
    return await _local_page_helper(title)


async def _diamond_left(title: str) -> list[ProofNote]:
    return await _local_page_helper(title)


async def _diamond_right(title: str) -> list[ProofNote]:
    return await _local_page_helper(title)


async def calls_local_helper(title: str) -> list[ProofNote]:
    return await _local_page_helper(title)


async def calls_nested_helper(title: str) -> list[ProofNote]:
    return await _nested_helper(title)


async def calls_diamond_helpers(
    title: str,
) -> tuple[list[ProofNote], list[ProofNote]]:
    return await _diamond_left(title), await _diamond_right(title)


async def _try_except_helper(id: int) -> ProofNote | None:
    try:
        return await ProofNote.aget(id=id)
    except ProofNote.NotFound:
        return None


async def _varargs_helper(*args: Any, **kwargs: Any) -> ProofNote | None:
    del args
    return await ProofNote.aget_or_none(**kwargs)


async def calls_try_except_and_varargs(
    id: int,
) -> tuple[ProofNote | None, ProofNote | None]:
    return await _try_except_helper(id), await _varargs_helper(id=id)


async def _self_recursive(n: int) -> int:
    if n <= 0:
        return 0
    return await _self_recursive(n - 1)


async def _mutual_a(n: int) -> int:
    return await _mutual_b(n)


async def _mutual_b(n: int) -> int:
    return await _mutual_a(n)


async def calls_self_recursive(n: int) -> int:
    return await _self_recursive(n)


async def calls_mutual_recursive(n: int) -> int:
    return await _mutual_a(n)


async def _unsafe_sleep_helper() -> None:
    await asyncio.sleep(0)


async def _unsafe_try_helper() -> ProofNote | None:
    try:
        await asyncio.sleep(0)
    except ProofNote.NotFound:
        return None
    return None


async def _unsafe_task_helper() -> None:
    asyncio.current_task()
    await ProofNote.aget(id=1)


async def _unsafe_hook_helper() -> Any:
    return await HookedNote.acreate(title="x")


def _wrap_helper(
    func: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        return await func(*args, **kwargs)

    return wrapper


@_wrap_helper
async def _decorated_helper(id: int) -> ProofNote:
    return await ProofNote.aget(id=id)


def _make_closure_helper() -> Callable[[int], Awaitable[ProofNote]]:
    model = ProofNote

    async def closed(id: int) -> ProofNote:
        return await model.aget(id=id)

    return closed


_closure_helper = _make_closure_helper()


async def calls_unsafe_sleep() -> None:
    await _unsafe_sleep_helper()


async def calls_unsafe_try() -> ProofNote | None:
    return await _unsafe_try_helper()


async def calls_unsafe_task() -> None:
    await _unsafe_task_helper()


async def calls_unsafe_hook() -> Any:
    return await _unsafe_hook_helper()


async def calls_decorated_helper(id: int) -> Any:
    return await _decorated_helper(id)


async def calls_closure_helper(id: int) -> ProofNote:
    return await _closure_helper(id)


async def calls_foreign_helper() -> None:
    await _foreign_helper(None, None, None)  # type: ignore[arg-type]


async def _depth_1() -> ProofNote:
    return await ProofNote.aget(id=1)


async def _depth_2() -> ProofNote:
    return await _depth_1()


async def _depth_3() -> ProofNote:
    return await _depth_2()


async def _depth_4() -> ProofNote:
    return await _depth_3()


async def _depth_5() -> ProofNote:
    return await _depth_4()


async def _depth_6() -> ProofNote:
    return await _depth_5()


async def _depth_7() -> ProofNote:
    return await _depth_6()


async def _depth_8() -> ProofNote:
    return await _depth_7()


async def _depth_9() -> ProofNote:
    return await _depth_8()


async def calls_at_depth_limit() -> ProofNote:
    return await _depth_8()


async def calls_past_depth_limit() -> ProofNote:
    return await _depth_9()


@pytest.mark.parametrize(
    "handler",
    [
        calls_local_helper,
        calls_nested_helper,
        calls_diamond_helpers,
        calls_try_except_and_varargs,
        calls_at_depth_limit,
    ],
)
def test_safe_module_local_helpers_are_proved(handler: Callable[..., Any]) -> None:
    assert native_refusal(handler) is None


@pytest.mark.parametrize(
    ("handler", "reason"),
    [
        (calls_self_recursive, "_self_recursive is recursive"),
        (calls_mutual_recursive, "_mutual_a is recursive"),
        (calls_past_depth_limit, "helper call depth exceeded"),
        (calls_unsafe_sleep, "an await is not on a Model class"),
        (calls_unsafe_try, "an await is not on a Model class"),
        (calls_unsafe_task, "it uses asyncio directly"),
        (calls_unsafe_hook, "HookedNote has write hooks"),
        (calls_decorated_helper, "_decorated_helper captures closure variables"),
        (calls_closure_helper, "_closure_helper captures closure variables"),
        (calls_foreign_helper, "_foreign_helper is not defined in this module"),
    ],
)
def test_unsafe_or_non_local_helpers_are_refused(
    handler: Callable[..., Any], reason: str
) -> None:
    assert native_refusal(handler) == reason
