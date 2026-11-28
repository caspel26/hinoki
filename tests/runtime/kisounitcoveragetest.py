"""Unit coverage for the Rust server's Python protocol adapter boundaries."""

from __future__ import annotations

import asyncio
import json
import logging
import runpy
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from hinoki import core
from hinoki.errors import InvalidBody
from hinoki.runtime import kiso
from hinoki.runtime.responses import HTTPError
from hinoki.settings import ServerSettings


class _Request:
    def __init__(self, **values: Any) -> None:
        defaults = {
            "http_version": "1.1",
            "method": "GET",
            "path": "/notes%20and%20more",
            "query": b"page=2",
            "headers": [(b"host", b"example.test")],
            "client": ("127.0.0.1", 1234),
            "request_id": "rid-1",
            "id_ignored": False,
            "action": None,
            "params": {},
            "native_handler": False,
            "native_args": (),
            "native_error": None,
            "_native_failure": None,
            "_native_rows": None,
        }
        defaults.update(values)
        vars(self).update(defaults)
        self._native_failure: Any = defaults["_native_failure"]
        self._native_rows: Any = defaults["_native_rows"]
        self.responses: list[tuple[Any, ...]] = []

    async def body(self, _limit: int) -> bytes:
        return b"payload"

    def _take_native_failure(self) -> Any:
        failure = self._native_failure
        self._native_failure = None
        return failure

    def _take_native_rows(self) -> Any:
        rows = self._native_rows
        self._native_rows = None
        return rows

    def respond(self, *values: Any) -> None:
        self.responses.append(values)

    def respond_final(self, *values: Any) -> None:
        self.responses.append(values)

    def respond_final_json(
        self, status: int, headers: Any, value: object, strict: bool = False
    ) -> None:
        body = core.encode_json(value, None, False, None)
        self.respond_final(status, headers, body)

    def respond_routed(self, *values: Any) -> None:
        self.responses.append(values)


def _worker() -> Any:
    worker: Any = object.__new__(kiso.KisoWorker)
    worker.host = "127.0.0.1"
    worker.port = 8000
    worker.address = (worker.host, worker.port)
    worker.scheme = "http"
    worker.max_connections = 100
    worker.native_dispatch = False
    worker.inline_dispatch = False
    worker.access_log = None
    worker.access_log_skip = None
    worker.access_log_slow_ms = None
    worker.access_log_sink = None
    worker.server_timing = False
    worker.compression = None
    worker.metrics = None
    worker.access_log_sink = None
    worker.lane = None
    worker.header_read_timeout = 5.0
    worker.body_read_timeout = 20.0
    worker.write_timeout = 30.0
    worker.tls_cert_file = None
    worker.tls_key_file = None
    worker.http2 = False
    worker.backlog = 128
    worker.server = None
    worker.timer = None
    worker.parent = None
    worker.tasks = set()
    worker.stopping = asyncio.Event()
    worker._lifespan_task = None
    worker.listener_fd = None
    worker.ready_fd = None
    worker.pipeline = SimpleNamespace()
    worker.core = SimpleNamespace()
    worker._inbox = asyncio.Queue()
    worker._outbox = asyncio.Queue()
    return worker


def test_worker_constructor_owns_adapter_limits_queues_and_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = SimpleNamespace(bindings=())
    adapter = SimpleNamespace(pipeline=pipeline)
    app = SimpleNamespace(asgi=lambda: adapter)
    monkeypatch.setattr(
        "hinoki.runtime.kiso.build_server_core", lambda _pipeline: "core"
    )
    worker: Any = kiso.KisoWorker(
        cast(Any, app),
        "127.0.0.1",
        8010,
        max_connections=42,
        header_read_timeout=1.5,
        body_read_timeout=2.5,
        write_timeout=3.5,
        tls_cert_file="cert.pem",
        tls_key_file="key.pem",
        http2=True,
        backlog=96,
        listener_fd=12,
        parent=34,
    )
    assert worker.adapter is adapter
    assert worker.pipeline is pipeline
    assert worker.core == "core"
    assert worker.max_connections == 42
    assert worker.header_read_timeout == 1.5
    assert worker.body_read_timeout == 2.5
    assert worker.write_timeout == 3.5
    assert worker.scheme == "https" and worker.http2
    assert worker.backlog == 96 and worker.listener_fd == 12 and worker.parent == 34
    assert isinstance(worker._inbox, asyncio.Queue)
    assert isinstance(worker._outbox, asyncio.Queue)


@pytest.mark.asyncio
async def test_lifespan_starts_one_adapter_task_and_finishes_it() -> None:
    worker = _worker()
    events: list[str] = []

    async def adapter(_scope: Any, receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            event = message["type"].removeprefix("lifespan.")
            events.append(event)
            await send({"type": f"lifespan.{event}.complete"})
            if event == "shutdown":
                return

    worker.adapter = adapter
    assert (await worker._lifespan("startup"))["type"] == "lifespan.startup.complete"
    task = worker._lifespan_task
    assert task is not None
    assert (await worker._lifespan("shutdown"))["type"] == "lifespan.shutdown.complete"
    await worker._finish_lifespan()
    assert events == ["startup", "shutdown"]
    assert worker._lifespan_task is None
    assert task.done()


@pytest.mark.parametrize(
    ("kind", "status", "code"),
    [
        ("too_large", 413, "payload_too_large"),
        ("timeout", 408, "request_timeout"),
        ("malformed", None, "disconnected"),
        ("disconnected", None, "disconnected"),
        ("other", None, "disconnected"),
    ],
)
def test_body_failure_maps_native_read_kinds(
    kind: str, status: int | None, code: str
) -> None:
    error = kiso.body_failure(kind)
    if status is not None:
        assert isinstance(error, HTTPError)
        assert error.response.status == status and str(error) == code
    else:
        assert isinstance(error, InvalidBody)
        assert error.code == code


@pytest.mark.asyncio
async def test_rust_body_returns_bytes_and_translates_native_error() -> None:
    request: Any = _Request()
    assert await kiso.RustBody(cast(Any, request)).read(16) == b"payload"

    async def fail(_limit: int) -> bytes:
        raise core.BodyReadError("timeout")

    request.body = fail
    with pytest.raises(HTTPError) as caught:
        await kiso.RustBody(cast(Any, request)).read(16)
    assert caught.value.response.status == 408


def test_scope_of_decodes_path_and_preserves_wire_values() -> None:
    request = _Request(path="/caf%C3%A9", http_version="2")
    scope = kiso.scope_of(cast(Any, request), ("0.0.0.0", 8443), "https")
    assert scope == {
        "type": "http",
        "http_version": "2",
        "method": "GET",
        "scheme": "https",
        "path": "/café",
        "raw_path": b"/caf%C3%A9",
        "query_string": b"page=2",
        "headers": [(b"host", b"example.test")],
        "client": ("127.0.0.1", 1234),
        "server": ("0.0.0.0", 8443),
    }


def test_housekeeping_reports_invalid_ids_and_dead_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker: Any = _worker()
    worker.core = SimpleNamespace(take_ignored_ids=lambda: 3)
    worker.pipeline = SimpleNamespace(crud_plan_selections=())
    worker.parent = 100
    monkeypatch.setattr("hinoki.runtime.kiso.os.getppid", lambda: 101)
    worker.housekeeping()
    assert worker.stopping.is_set()


@pytest.mark.asyncio
async def test_wake_removes_dead_reader_and_retries_failed_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker: Any = _worker()
    calls: list[tuple[str, Any]] = []
    loop = SimpleNamespace(
        remove_reader=lambda fd: calls.append(("remove", fd)),
        call_soon=lambda callback, *args: calls.append(("retry", (callback, args))),
    )
    monkeypatch.setattr("hinoki.runtime.kiso.asyncio.get_running_loop", lambda: loop)
    server = SimpleNamespace(wake_fd=12, clear_wake=lambda: False)
    worker._wake(cast(Any, server))
    assert calls == [("remove", 12)]

    calls.clear()
    server.clear_wake = lambda: True

    def fail_drain() -> None:
        raise RuntimeError("drain failed")

    monkeypatch.setattr(worker, "_drain", fail_drain)
    worker._wake(cast(Any, server))
    assert calls and calls[0][0] == "retry"


def test_drain_fails_individual_spawn_and_completes_short_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker: Any = _worker()
    request = _Request()
    batches = [[request], []]
    worker.core = SimpleNamespace(drain=lambda _limit: batches.pop(0))
    spawned: list[Any] = []
    failed: list[Any] = []

    def spawn(item: Any) -> None:
        spawned.append(item)
        raise RuntimeError("cannot start")

    def fail(item: Any) -> None:
        failed.append(item)

    monkeypatch.setattr(worker, "_spawn", spawn)
    monkeypatch.setattr(worker, "_fail", fail)
    worker._drain()
    assert spawned == [request] and failed == [request]


@pytest.mark.asyncio
async def test_spawn_tracks_and_discards_request_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker: Any = _worker()
    finished = asyncio.Event()

    async def serve(_request: Any) -> None:
        finished.set()

    monkeypatch.setattr(worker, "_serve", serve)
    worker._spawn(cast(Any, _Request()))
    await asyncio.wait_for(finished.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not worker.tasks


@pytest.mark.asyncio
async def test_fail_uses_pipeline_headers_suppresses_head_body_and_survives_write_error(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker: Any = _worker()
    worker.pipeline = SimpleNamespace(
        with_headers=lambda response, _scope, _rid: SimpleNamespace(
            status=response.status, headers=[(b"x-test", b"yes")], body=b"error"
        )
    )
    request = _Request(method="HEAD")
    worker._fail(cast(Any, request))
    assert request.responses == [(500, [(b"x-test", b"yes")], b"")]

    def fail_headers(*_args: Any) -> Any:
        raise RuntimeError("write failed")

    worker.pipeline.with_headers = fail_headers
    worker._fail(cast(Any, _Request()))
    assert "could not be answered" in caplog.text


@pytest.mark.asyncio
async def test_serve_handles_pipeline_errors_head_and_native_final_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker: Any = _worker()
    response = SimpleNamespace(status=200, headers=[], body=b"answer", stream=None)
    worker.pipeline.handle_unfinished = lambda *_args, **_kwargs: asyncio.sleep(
        0, result=response
    )
    worker.pipeline.handle_native = lambda *_args: asyncio.sleep(0, result=response)
    failed: list[Any] = []

    def fail(request: Any) -> None:
        failed.append(request)

    monkeypatch.setattr(worker, "_fail", fail)

    normal = _Request(method="HEAD", id_ignored=True)
    await worker._serve(cast(Any, normal))
    assert normal.responses == [(200, [], b"answer", ())]

    native = _Request(native_handler=True, action="native")
    worker.pipeline.bindings = {
        "native": SimpleNamespace(
            route=SimpleNamespace(
                method="GET", access=None, rate_limit=None, renderers=[]
            ),
            query_param=None,
        )
    }
    await worker._serve(cast(Any, native))
    assert native.responses == [(200, [], b"answer")]

    missing_action = _Request(native_handler=True, action=None)
    await worker._serve(cast(Any, missing_action))
    assert failed == [missing_action]

    async def pipeline_error(*_args: Any) -> Any:
        raise RuntimeError("handler error")

    worker.pipeline.handle_unfinished = pipeline_error
    broken = _Request()
    await worker._serve(cast(Any, broken))
    assert failed == [missing_action, broken]


@pytest.mark.asyncio
async def test_native_cold_paths_reject_requests_without_actions() -> None:
    worker: Any = _worker()
    request = _Request(action=None)
    with pytest.raises(RuntimeError, match="no action or failure"):
        await worker._serve_native_failure(
            cast(Any, request), ("database", "failed", None, None, None)
        )
    with pytest.raises(RuntimeError, match="no action or rows"):
        await worker._serve_native_rows(cast(Any, request), ([], None))


@pytest.mark.asyncio
async def test_stop_without_server_and_with_shutdown_callbacks(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker: Any = _worker()
    assert await worker.stop() == 0

    class Server:
        wake_fd = 9

        async def shutdown(self) -> int:
            return 2

    events: list[str] = []
    worker.server = cast(Any, Server())
    worker.timer = SimpleNamespace(cancel=lambda: events.append("timer"))
    worker.core = SimpleNamespace(
        set_ready=lambda ready: events.append(f"ready:{ready}"),
        _clear_inline_lane=lambda: None,
        _disarm_crud_plans=lambda: None,
        _detach_database=lambda: None,
    )
    worker.pipeline = SimpleNamespace(
        app=SimpleNamespace(runtime=SimpleNamespace(shutdown_timeout=1.0)),
        streams=SimpleNamespace(stop_events=lambda: events.append("streams")),
    )
    drain_results = [(2, False), (0, True)]
    monkeypatch.setattr(
        kiso,
        "_drain_native_uninterruptibly",
        lambda *_args: asyncio.sleep(0, result=drain_results.pop(0)),
    )

    def fail_housekeeping() -> None:
        raise RuntimeError("final counter pass failed")

    worker.housekeeping = fail_housekeeping
    worker._cancel_requests = lambda: asyncio.sleep(0, result=None)
    worker._lifespan = lambda event: asyncio.sleep(0, result={"type": event})
    worker._finish_lifespan = lambda: asyncio.sleep(0, result=None)
    loop = SimpleNamespace(remove_reader=lambda fd: events.append(f"reader:{fd}"))
    monkeypatch.setattr("hinoki.runtime.kiso.asyncio.get_running_loop", lambda: loop)
    assert await worker.stop() == 2
    assert events == ["timer", "streams", "ready:False", "reader:9"]
    assert "aborted 2 native CRUD jobs" in caplog.text

    worker.server = cast(Any, Server())
    with pytest.raises(asyncio.CancelledError):
        await worker.stop()


@pytest.mark.asyncio
async def test_start_failure_paths_never_leave_lifespan_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker: Any = _worker()
    events: list[str] = []

    async def startup_failure(_event: str) -> dict[str, str]:
        return {"type": "lifespan.startup.failed", "message": "not ready"}

    async def finish() -> None:
        events.append("finish")

    monkeypatch.setattr(worker, "_lifespan", startup_failure)
    monkeypatch.setattr(worker, "_finish_lifespan", finish)
    with pytest.raises(RuntimeError, match="startup failed: not ready"):
        await worker.start()
    assert events == ["finish"]

    async def startup_ok(_event: str) -> dict[str, str]:
        return {"type": "lifespan.startup.complete"}

    worker._lifespan = startup_ok
    worker._finish_lifespan = finish
    worker.pipeline = SimpleNamespace(
        crud_plan_selections=(),
        app=SimpleNamespace(
            runtime=SimpleNamespace(
                shutdown_timeout=4.0, max_header_bytes=8192, request_timeout=20.0
            )
        ),
    )
    worker.listener_fd = 11
    worker.core = SimpleNamespace(
        _disarm_crud_plans=lambda: None,
        _detach_database=lambda: None,
    )
    monkeypatch.setattr(
        kiso,
        "_drain_native_uninterruptibly",
        lambda *_args: asyncio.sleep(0, result=(0, False)),
    )

    class FailedServer:
        @staticmethod
        def start(*args: Any) -> Any:
            assert args[7] == 10.0
            raise RuntimeError("native startup failed")

    monkeypatch.setattr(kiso, "core", SimpleNamespace(KisoServer=FailedServer))
    events.clear()

    async def track_lifespan(event: str) -> dict[str, str]:
        events.append(event)
        return {"type": f"lifespan.{event}.complete"}

    worker._lifespan = track_lifespan
    with pytest.raises(RuntimeError, match="native startup failed"):
        await worker.start()
    assert events == ["startup", "shutdown", "finish"]


@pytest.mark.asyncio
async def test_start_binds_only_after_success_and_registers_housekeeping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker: Any = _worker()
    worker.pipeline = SimpleNamespace(
        crud_plan_selections=(),
        bindings=[],
        app=SimpleNamespace(
            runtime=SimpleNamespace(
                shutdown_timeout=4.0, max_header_bytes=8192, request_timeout=20.0
            )
        ),
    )
    worker.body_read_timeout = 8.0
    worker.write_timeout = 30.0
    worker.max_connections = 100
    worker.header_read_timeout = 2.0
    worker.tls_cert_file = None
    worker.tls_key_file = None
    worker.http2 = False
    worker.backlog = 128
    worker.listener_fd = None
    worker.parent = None
    worker.core = SimpleNamespace(
        set_ready=lambda ready: events.append(f"ready:{ready}"),
        _clear_inline_lane=lambda: None,
        _disarm_crud_plans=lambda: None,
        _detach_database=lambda: None,
    )
    monkeypatch.setattr(
        kiso,
        "_drain_native_uninterruptibly",
        lambda *_args: asyncio.sleep(0, result=(0, False)),
    )
    events: list[Any] = []
    server = SimpleNamespace(port=9001, wake_fd=17)

    class NativeServer:
        @staticmethod
        def start(*args: Any) -> Any:
            events.append(("start", args[1:]))
            return server

    class Listener:
        def detach(self) -> int:
            events.append("detach")
            return 33

    monkeypatch.setattr(kiso, "core", SimpleNamespace(KisoServer=NativeServer))
    monkeypatch.setattr(
        "hinoki.runtime.kiso.open_listener", lambda *_args, **_kw: Listener()
    )
    monkeypatch.setattr(
        worker,
        "_lifespan",
        lambda event: asyncio.sleep(0, result={"type": f"lifespan.{event}.complete"}),
    )
    monkeypatch.setattr(worker, "_finish_lifespan", lambda: asyncio.sleep(0))

    def add_reader(fd: int, callback: Any, *args: Any) -> None:
        assert callable(callback) and args
        events.append(("reader", fd))

    def call_later(delay: float, callback: Any) -> object:
        assert callback == worker._tick
        events.append(("timer", delay))
        return object()

    loop = SimpleNamespace(add_reader=add_reader, call_later=call_later)
    monkeypatch.setattr("hinoki.runtime.kiso.asyncio.get_running_loop", lambda: loop)
    assert await worker.start() == 9001
    assert worker.listener_fd is None
    assert worker.server is server
    assert ("reader", 17) in events
    assert ("timer", kiso.HOUSEKEEPING) in events
    assert events[-1] == "ready:True"


def test_tick_logs_and_reschedules_even_when_housekeeping_fails(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker: Any = _worker()
    events: list[Any] = []

    def fail() -> None:
        raise RuntimeError("housekeeping broke")

    worker.housekeeping = fail
    loop = SimpleNamespace(
        call_later=lambda delay, callback: events.append((delay, callback))
    )
    monkeypatch.setattr("hinoki.runtime.kiso.asyncio.get_running_loop", lambda: loop)
    worker._tick()
    assert "housekeeping failed" in caplog.text
    assert events == [(kiso.HOUSEKEEPING, worker._tick)]


@pytest.mark.asyncio
async def test_cancel_requests_cancels_and_joins_every_tracked_task() -> None:
    worker: Any = _worker()
    started = asyncio.Event()

    async def pending() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(pending())
    worker.tasks.add(task)
    await started.wait()
    await worker._cancel_requests()
    assert task.cancelled()
    assert worker.tasks == {task}


def test_signal_handling_is_graceful_until_unmanaged_second_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker: Any = _worker()
    worker._on_signal()
    assert worker.stopping.is_set()
    exits: list[int] = []

    def exit_process(code: int) -> None:
        exits.append(code)

    monkeypatch.setattr("hinoki.runtime.kiso.os._exit", exit_process)
    worker._on_signal()
    assert exits == [1]

    supervised: Any = _worker()
    supervised.parent = 42
    supervised.stopping.set()
    supervised._on_signal()
    assert exits == [1]
    supervised._ignore_signal()


@pytest.mark.asyncio
async def test_run_registers_signals_waits_and_logs_aborted_connections(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker: Any = _worker()
    worker.host = "::1"
    worker.scheme = "https"
    worker.stopping.set()
    events: list[Any] = []
    loop = SimpleNamespace(
        add_signal_handler=lambda sig, callback: events.append((sig, callback))
    )
    monkeypatch.setattr("hinoki.runtime.kiso.asyncio.get_running_loop", lambda: loop)
    monkeypatch.setattr(worker, "start", lambda: asyncio.sleep(0, result=9555))
    monkeypatch.setattr(worker, "stop", lambda: asyncio.sleep(0, result=2))
    assert await worker.run() == 0
    assert "listening on https://[::1]:9555" in capsys.readouterr().out
    assert len(events) == 3
    assert "aborted 2 connections" in caplog.text


def test_build_worker_passes_only_native_server_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app: Any = object()
    settings = SimpleNamespace(
        backlog=64,
        tls_cert_file="cert.pem",
        tls_key_file="key.pem",
        http2=True,
        max_connections=200,
        header_read_timeout=None,
        body_read_timeout=3.0,
        write_timeout=None,
        native_dispatch=None,
        access_log_format=None,
        access_log_skip=None,
        access_log_slow_ms=None,
        recent_requests=None,
        server_timing=None,
        logs_access=lambda dev: dev,
    )
    captured: dict[str, Any] = {}

    def capture(*args: Any, **kwargs: Any) -> Any:
        captured["args"] = args
        captured.update(kwargs)
        return "worker"

    monkeypatch.setattr(kiso, "KisoWorker", capture)
    result: Any = kiso.build_worker(
        app, "127.0.0.1", 88, cast(Any, settings), listener_fd=4, parent=5
    )
    assert result == "worker"
    assert captured["args"] == (app, "127.0.0.1", 88)
    assert captured["backlog"] == 64
    assert captured["tls_cert_file"] == "cert.pem"
    assert captured["max_connections"] == 200
    assert captured["body_read_timeout"] == 3.0
    assert captured["listener_fd"] == 4 and captured["parent"] == 5
    assert captured["access_log"] is None


def test_main_checks_parent_and_runs_loaded_factory_with_inherited_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = 123
    monkeypatch.setattr("hinoki.runtime.kiso.os.getppid", lambda: 999)
    assert kiso.main(["sample:app", "--parent", str(parent)]) == 1

    monkeypatch.setattr("hinoki.runtime.kiso.os.getppid", lambda: parent)
    events: list[Any] = []
    monkeypatch.setattr(
        "hinoki.runtime.kiso.os.set_inheritable",
        lambda fd, enabled: events.append(("inheritable", fd, enabled)),
    )

    def inherited_address(fd: int) -> tuple[str, int]:
        events.append(("address", fd))
        return "::1", 9443

    monkeypatch.setattr("hinoki.runtime.kiso.inherited_address", inherited_address)
    app = SimpleNamespace(settings=object())
    monkeypatch.setattr(
        "hinoki.runtime.kiso.load_target", lambda _target: (lambda: app, True)
    )
    monkeypatch.setattr(
        "hinoki.runtime.kiso.settings_of",
        lambda _target: SimpleNamespace(runtime_threads=2),
    )
    monkeypatch.setattr(
        kiso,
        "core",
        SimpleNamespace(
            configure_runtime=lambda count: events.append(("threads", count))
        ),
    )

    class Worker:
        async def run(self) -> int:
            events.append("run")
            return 0

    def build(
        target: Any, host: str, port: int, _settings: Any, **options: Any
    ) -> Worker:
        events.append((target, host, port, options))
        return Worker()

    monkeypatch.setattr(kiso, "build_worker", build)
    assert kiso.main(["sample:factory", "--fd", "7", "--parent", str(parent)]) == 0
    assert events[0] == ("inheritable", 7, False)
    assert ("address", 7) in events
    assert any(
        isinstance(item, tuple) and item[:3] == (app, "::1", 9443) for item in events
    )
    assert "run" in events


def test_module_entrypoint_exits_with_main_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("hinoki.core.configure_runtime", lambda _threads: None)
    monkeypatch.setattr(
        "hinoki.runtime.servercore.build_server_core", lambda _pipeline: object()
    )
    app = SimpleNamespace(
        asgi=lambda: SimpleNamespace(pipeline=SimpleNamespace(bindings=())),
        settings=ServerSettings(),
    )
    monkeypatch.setattr("hinoki.serve.load_target", lambda _target: (app, False))
    monkeypatch.setattr("hinoki.serve.settings_of", lambda _app: ServerSettings())

    def close_coroutine(coroutine: Any) -> int:
        coroutine.close()
        return 17

    monkeypatch.setattr("asyncio.run", close_coroutine)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hinoki.runtime.kiso", "sample:app"],
    )
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="'hinoki.runtime.kiso' found in sys.modules",
            category=RuntimeWarning,
        )
        with pytest.raises(SystemExit) as caught:
            runpy.run_module("hinoki.runtime.kiso", run_name="__main__")
    assert caught.value.code == 17


def test_log_access_attaches_the_parsed_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    record = {
        "peer": "10.0.0.1:5",
        "method": "GET",
        "path": "/x",
        "status": 200,
        "duration_ms": 1.25,
        "lane": "native",
    }
    with caplog.at_level(logging.INFO, logger="hinoki.access"):
        kiso.log_access([json.dumps(record)])
    assert caplog.records[0].getMessage() == "10.0.0.1:5 GET /x 200 1.2ms"
    assert caplog.records[0].access == record  # type: ignore[attr-defined]


def test_a_route_auth_snapshot_that_cannot_be_checked_is_stale() -> None:
    worker: Any = _worker()
    worker.pipeline = SimpleNamespace(bindings={0: object()})
    assert worker._route_auth_stale(0, (None, ())) is True


def _recording_fail(worker: Any, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    failed: list[Any] = []
    monkeypatch.setattr(worker, "_fail", failed.append)
    return failed


@pytest.mark.asyncio
async def test_a_websocket_request_without_an_action_is_failed_not_dropped(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker: Any = _worker()
    failed = _recording_fail(worker, monkeypatch)
    request = _Request(method="WS", action=None, id_ignored=True)
    await worker._serve_websocket(cast(Any, request))
    assert failed == [request]
    assert "ignored invalid request ID" in caplog.text
    assert "request rid-1 failed" in caplog.text


@pytest.mark.asyncio
async def test_a_translated_native_failure_is_sent_or_failed(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker: Any = _worker()
    failed = _recording_fail(worker, monkeypatch)
    response = SimpleNamespace(status=409, headers=[(b"x", b"1")], body=b"conflict")
    worker._serve_native_failure = lambda *_args: asyncio.sleep(0, result=response)
    sent = _Request(id_ignored=True)
    await worker._serve_failure(cast(Any, sent), cast(Any, None))
    assert sent.responses == [(409, [(b"x", b"1")], b"conflict")]
    assert "ignored invalid request ID" in caplog.text

    async def broken(*_args: Any) -> Any:
        raise RuntimeError("translation failed")

    worker._serve_native_failure = broken
    lost = _Request()
    await worker._serve_failure(cast(Any, lost), cast(Any, None))
    assert failed == [lost]


def test_a_native_failure_is_answered_in_place_only_when_nothing_can_match(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker: Any = _worker()
    failed = _recording_fail(worker, monkeypatch)
    failure = cast(Any, None)
    assert worker._answer_failure(cast(Any, _Request(action=None)), failure) is False

    answers: list[Any] = [RuntimeError("boom"), None]

    def crud_failure_now(*_args: Any) -> Any:
        answer = answers.pop(0) if answers else response
        if isinstance(answer, Exception):
            raise answer
        return answer

    response = SimpleNamespace(status=503, headers=[], body=b"later")
    worker.pipeline = SimpleNamespace(crud_failure_now=crud_failure_now)
    raising = _Request(action=1)
    assert worker._answer_failure(cast(Any, raising), failure) is True
    assert failed == [raising]
    assert worker._answer_failure(cast(Any, _Request(action=1)), failure) is False

    answered = _Request(action=1, id_ignored=True)
    assert worker._answer_failure(cast(Any, answered), failure) is True
    assert answered.responses == [(503, [], b"later")]
    assert "ignored invalid request ID" in caplog.text

    class Gone(_Request):
        def respond_final(self, *values: Any) -> None:
            raise OSError("client gone")

    gone = Gone(action=1)
    assert worker._answer_failure(cast(Any, gone), failure) is True
    assert failed == [raising, gone]
    assert "could not be answered" in caplog.text


@pytest.mark.asyncio
async def test_a_request_queued_during_the_lane_drain_is_cancelled_too() -> None:
    worker: Any = _worker()
    late: list[asyncio.Task[None]] = []

    async def pending() -> None:
        await asyncio.Event().wait()

    async def drain() -> None:
        task = asyncio.create_task(pending())
        late.append(task)
        worker.tasks.add(task)

    worker.lane = SimpleNamespace(cancel_all=lambda: None, unwinding=set(), drain=drain)
    await worker._cancel_requests()
    assert late[0].cancelled()


def test_access_records_reach_the_logger_and_the_recent_requests_file(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    recent = tmp_path / "recent.jsonl"
    settings = ServerSettings(access_log_format="logger", recent_requests=str(recent))
    sink = kiso.access_sink(settings, False)
    assert sink is not None
    line = json.dumps(
        {
            "peer": "10.0.0.1:5",
            "method": "GET",
            "path": "/x",
            "status": 200,
            "duration_ms": 1.0,
            "lane": "native",
        }
    )
    with caplog.at_level(logging.INFO, logger="hinoki.access"):
        sink([line])
    assert recent.read_text().splitlines() == [line]
    assert [r.getMessage() for r in caplog.records] == ["10.0.0.1:5 GET /x 200 1.0ms"]
