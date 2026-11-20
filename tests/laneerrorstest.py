"""A handler the native lane drives that raises is answered in place when no
application error handler can match; one that can match is translated in a
task. The asyncio path (``native_dispatch=False``) is the oracle."""

import re
from collections.abc import Callable
from typing import Any

import pytest

from hinoki import Hinoki, http
from hinoki.core import UniqueViolation
from hinoki.errors import APIError, Conflict, ObjectNotFound
from hinoki.runtime.kiso import KisoWorker
from hinoki.settings import AppSettings
from tests.kisoservertest import Received, fetch


class Teapot(Exception):
    pass


@http.get("/err/value")
async def raise_value() -> dict[str, str]:
    raise ValueError("boom")


@http.get("/err/api")
async def raise_api() -> dict[str, str]:
    raise Conflict()


@http.get("/err/missing")
async def raise_missing() -> dict[str, str]:
    raise ObjectNotFound("nothing here")


@http.get("/err/unique")
async def raise_unique() -> dict[str, str]:
    raise UniqueViolation("taken")


@http.get("/err/teapot")
async def raise_teapot() -> dict[str, str]:
    raise Teapot("short and stout")


PATHS = ["/err/value", "/err/api", "/err/missing", "/err/unique", "/err/teapot"]


class Brewing(APIError):
    status = 418
    code = "teapot"
    message = "I am a teapot."


def sync_handler(_error: Teapot) -> APIError:
    return Brewing()


async def async_handler(_error: Teapot) -> APIError:
    return Brewing()


def failing_handler(_error: Teapot) -> APIError:
    raise RuntimeError("handler failed")


def wrong_handler(_error: Teapot) -> Any:
    return "not an error"


def unique_handler(_error: UniqueViolation) -> APIError:
    return Brewing()


def make_app(register: Callable[[Hinoki], None] | None, debug: bool = False) -> Hinoki:
    app = Hinoki(settings=AppSettings(debug=debug))
    app.include(raise_value, raise_api, raise_missing, raise_unique, raise_teapot)
    if register is not None:
        register(app)
    return app


def teapot(handler: Callable[[Teapot], Any]) -> Callable[[Hinoki], None]:
    return lambda app: app.errors.add(Teapot, handler)


def normalized(received: Received) -> tuple[int, list[tuple[str, str]], bytes]:
    skipped = {"x-request-id", "date"}
    if b'"debug"' in received.body:
        skipped.add("content-length")  # the traceback differs by frame
    headers = [(k, v) for k, v in received.headers if k not in skipped]
    ids = re.compile(rb"[0-9a-f]{32}")
    traces = re.compile(rb'"debug":.*?\}(?=\})', re.S)
    body = traces.sub(b'"debug":<trace>', ids.sub(b"<id>", received.body))
    return received.status, headers, body


async def serve(
    register: Callable[[Hinoki], None] | None,
    *,
    native: bool,
    debug: bool = False,
    late: Callable[[Hinoki], None] | None = None,
) -> tuple[list[Received], int]:
    app = make_app(register, debug)
    worker = KisoWorker(app, "127.0.0.1", 0, native_dispatch=native)
    port = await worker.start()
    if late is not None:
        late(app)
    tasks = 0
    if worker.lane is not None:
        track = worker.lane._track

        def counted(*args: Any) -> Any:
            nonlocal tasks
            tasks += 1
            return track(*args)

        worker.lane._track = counted  # type: ignore[method-assign,assignment]
    try:
        return [await fetch(port, "GET", path) for path in PATHS], tasks
    finally:
        await worker.stop()


CASES = {
    "none": (None, None, 0),
    "sync": (teapot(sync_handler), None, 1),
    "async": (teapot(async_handler), None, 1),
    "failing": (teapot(failing_handler), None, 1),
    "wrong": (teapot(wrong_handler), None, 1),
    "unique": (lambda app: app.errors.add(UniqueViolation, unique_handler), None, 1),
    "late": (None, teapot(async_handler), 1),
}


@pytest.mark.parametrize("debug", [False, True])
@pytest.mark.parametrize("case", sorted(CASES))
async def test_lane_errors_answer_like_the_asyncio_path(case: str, debug: bool) -> None:
    register, late, deferred = CASES[case]
    oracle, _ = await serve(register, native=False, debug=debug, late=late)
    served, tasks = await serve(register, native=True, debug=debug, late=late)
    assert [normalized(r) for r in served] == [normalized(r) for r in oracle]
    assert tasks == deferred
