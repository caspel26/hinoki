"""A deferred ``list[dict[str, object]]`` answer is checked while Rust encodes it
(``Response.strict``). Every payload must end exactly as the checked two-pass
path ends: the same bytes, the same ``ResponseContractError`` or encode error."""

import datetime
import math
import re
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from hinoki import Hinoki, core, http
from hinoki.runtime.kiso import KisoWorker
from hinoki.runtime.nativelane import NativeLane, Run
from hinoki.runtime.responsecheck import ResponseContractError
from tests.kisoservertest import Received, fetch

Rows = list[dict[str, object]]


class Key(str):
    pass


class Row(dict[str, object]):
    pass


class Rows2(list[dict[str, object]]):
    pass


def valid() -> Rows:
    return [
        {
            "id": index,
            "name": f'café üñ \U0001f600 "q" \\ {index}\n',
            "score": index / 7,
            "big": 10**30,
            "flags": [True, False, None, (1, 2)],
            "nested": {"a": {"b": [1.5, {"c": ""}]}},
            "empty": {},
        }
        for index in range(30)
    ] + [{}, {"": ""}]


PAYLOADS: dict[str, Callable[[], object]] = {
    "good": valid,
    "empty": list,
    "subclass-list": lambda: Rows2(valid()),
    "subclass-item": lambda: [{"a": 1}, Row(b=2)],
    "datetime-value": lambda: [{"at": datetime.datetime(2026, 1, 2, 3, 4, 5)}],
    "nested-int-key": lambda: [{"a": {1: 2}}],
    "nan": lambda: [{"a": math.nan}],
    "nan-then-bad-key": lambda: [{"a": math.nan}, {}, {1: "x"}],
    "str-subclass-key": lambda: [{"a": 1}, {Key("k"): 1}],
    "int-key": lambda: [{"a": 1}, {1: "v"}],
    "none-key": lambda: [{None: 1}],
    "non-dict-item": lambda: [{"a": 1}, 1],
    "unsupported": lambda: [{"a": object()}],
}


def make_handler(name: str) -> Callable[[], Any]:
    async def handler() -> Rows:
        return PAYLOADS[name]()  # type: ignore[return-value]

    handler.__name__ = "route_" + name.replace("-", "_")
    return handler


def make_app() -> Hinoki:
    app = Hinoki()
    app.include(*(http.get(f"/fused/{name}")(make_handler(name)) for name in PAYLOADS))
    return app


def normalized(received: Received) -> tuple[int, bytes]:
    return received.status, re.sub(rb"[0-9a-f]{32}", b"<id>", received.body)


async def serve(native: bool) -> tuple[list[Received], list[int]]:
    worker = KisoWorker(make_app(), "127.0.0.1", 0, native_dispatch=native)
    port = await worker.start()
    started: list[int] = []
    if worker.lane is not None:
        begin = worker.lane.start

        def recorded(request: Any) -> bool:
            started.append(int(taken := begin(request)))
            return taken

        worker.lane.start = recorded  # type: ignore[method-assign]
    try:
        return [
            await fetch(port, "GET", f"/fused/{name}") for name in PAYLOADS
        ], started
    finally:
        await worker.stop()


async def test_the_lane_answers_every_payload_like_the_asyncio_path() -> None:
    oracle, none = await serve(False)
    served, started = await serve(True)
    assert none == []
    assert started == [1] * len(PAYLOADS)
    assert [normalized(r) for r in served] == [normalized(r) for r in oracle]
    by_name = dict(zip(PAYLOADS, served, strict=True))
    assert by_name["good"].body == core.encode_json(valid())
    assert by_name["empty"].body == b"[]"
    assert by_name["subclass-list"].body == core.encode_json(valid())
    for name in ("good", "empty", "subclass-item", "datetime-value", "nested-int-key"):
        assert by_name[name].status == 200
    for name in (
        "nan",
        "nan-then-bad-key",
        "str-subclass-key",
        "int-key",
        "none-key",
        "non-dict-item",
        "unsupported",
    ):
        assert by_name[name].status == 500


class Recorder:
    """A request whose strict encode always misses."""

    def __init__(self) -> None:
        self.request_id = "rid"
        self.calls: list[bool] = []

    def respond_final_json(
        self, status: int, headers: Any, value: object, strict: bool = False
    ) -> None:
        self.calls.append(strict)
        if strict:
            raise TypeError("miss")


def run_of(payload: object, check: Callable[[object], bool]) -> tuple[Run, Recorder]:
    binding = SimpleNamespace(
        response_check=check,
        response_schema=None,
        response_scan=None,
        route=SimpleNamespace(status=200, name="route"),
        response_annotation="rows",
    )
    request = Recorder()
    return Run(request, binding, None), request  # type: ignore[arg-type]


def test_a_miss_runs_the_contract_check_before_the_plain_encode() -> None:
    payload = [{"a": 1}]
    run, request = run_of(payload, lambda _: True)
    NativeLane._send_strict(run, 200, [], payload)
    assert request.calls == [True, False]


def test_a_contract_violation_after_a_miss_outranks_the_encode_error() -> None:
    payload = [{"a": 1}]
    run, request = run_of(payload, lambda _: False)
    with pytest.raises(ResponseContractError, match="does not match"):
        NativeLane._send_strict(run, 200, [], payload)
    assert request.calls == [True]


def binding_run(
    payload: object, check: Callable[[object], bool]
) -> tuple[Run, Recorder]:
    run, request = run_of(payload, check)
    object.__setattr__(run.binding, "fused_check", True)
    return run, request


def test_the_first_miss_turns_the_fused_path_off_for_the_binding() -> None:
    payload = [{"a": 1}]
    run, request = binding_run(payload, lambda _: True)
    NativeLane._send_strict(run, 200, [], payload)
    assert run.binding.fused_check is False
    assert request.calls == [True, False]


def test_a_contract_violation_also_turns_the_fused_path_off() -> None:
    payload = [{"a": 1}]
    run, _ = binding_run(payload, lambda _: False)
    for _ in range(2):
        with pytest.raises(ResponseContractError, match="does not match"):
            NativeLane._send_strict(run, 200, [], payload)
    assert run.binding.fused_check is False


async def test_unvouched_rows_leave_the_fused_path_after_one_request() -> None:
    app = Hinoki()
    app.include(
        http.get("/fused/dt")(make_handler("datetime-value")),
        http.get("/fused/ok")(make_handler("good")),
    )
    worker = KisoWorker(app, "127.0.0.1", 0, native_dispatch=True)
    port = await worker.start()
    flags: list[bool] = []
    assert worker.lane is not None
    send = worker.lane._send

    def spy(run: Run, response: Any) -> Exception | None:
        flags.append(bool(response.strict))
        return send(run, response)

    worker.lane._send = spy  # type: ignore[method-assign]
    try:
        first = [await fetch(port, "GET", "/fused/dt") for _ in range(2)]
        plain = [await fetch(port, "GET", "/fused/ok") for _ in range(20)]
    finally:
        await worker.stop()
    assert flags[:2] == [True, False]
    assert first[0].status == 200
    assert first[0].body == first[1].body
    assert all(r.body == core.encode_json(valid()) for r in plain)
    assert flags[2:] == [True] * 20
    # Both handlers share one qualified name, so read the bindings in order.
    assert [b.fused_check for b in worker.pipeline.bindings] == [False, True]
