"""Choosing and refusing the WSGI interface in ``hinoki serve``."""

import argparse
import importlib.util
import os
import signal
import threading

import pytest

from hinoki import Hinoki, core, serve
from hinoki.runtime import banner, supervisor, wsgiworker
from hinoki.runtime.listener import ListenError
from hinoki.serve import (
    load_wsgi_target,
    settings_of,
    stop_grace_of,
    wanted_interface,
    wsgi_refusal,
)
from hinoki.settings import AppSettings, ExecutionSettings, ServerSettings
from hinoki.wsgi import WSGIAdapter

PLAIN = "tests.runtime.wsgiscenarios:app"
ADAPTER = "tests.runtime.wsgilimitsapp:wsgi_app"
HINOKI = "tests.kisoserverapp:app"


def args(target: str, interface: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(target=target, interface=interface)


def test_a_plain_callable_loads_and_an_adapter_too() -> None:
    assert callable(load_wsgi_target(PLAIN))
    assert isinstance(load_wsgi_target(ADAPTER), WSGIAdapter)


def test_a_hinoki_application_points_to_the_adapter() -> None:
    with pytest.raises(SystemExit, match="WSGIAdapter"):
        load_wsgi_target(HINOKI)


def test_a_missing_or_non_callable_target_is_refused() -> None:
    with pytest.raises(SystemExit, match="cannot load"):
        load_wsgi_target("tests.nope:app")
    with pytest.raises(SystemExit, match="not a WSGI callable"):
        load_wsgi_target("tests.runtime.wsgiscenarios:CHUNKS")


def test_the_flag_wins_then_the_adapter_setting_then_asgi() -> None:
    assert wanted_interface(args(HINOKI, "wsgi")) == "wsgi"
    assert wanted_interface(args(PLAIN, "asgi")) == "asgi"
    assert wanted_interface(args(HINOKI)) == "asgi"
    assert wanted_interface(args(PLAIN)) == "asgi"
    assert wanted_interface(args("tests.nope:app")) == "asgi"


def test_an_adapter_whose_settings_say_wsgi_is_served_as_wsgi() -> None:
    app = Hinoki(settings=AppSettings(server=ServerSettings(interface="wsgi")))
    adapter = WSGIAdapter(app)
    assert settings_of(adapter).interface == "wsgi"
    assert settings_of(print).interface == "asgi"  # no settings: defaults


def test_what_kiso_cannot_host_is_named() -> None:
    assert wsgi_refusal(ServerSettings()) is None
    assert "native_dispatch" in (
        wsgi_refusal(ServerSettings(native_dispatch=True)) or ""
    )
    assert "trusted_proxies" in (
        wsgi_refusal(ServerSettings(trusted_proxies=("10.0.0.0/8",))) or ""
    )


def test_a_plain_callable_gets_the_default_stop_grace() -> None:
    assert stop_grace_of(load_wsgi_target(PLAIN)) == 3 * (
        AppSettings().lifecycle.shutdown_timeout
    )


def served_args(target: str, **extra: object) -> argparse.Namespace:
    values = {
        "target": target,
        "interface": "wsgi",
        "server": None,
        "workers": None,
        "host": None,
        "port": None,
    }
    return argparse.Namespace(**{**values, **extra})


@pytest.fixture
def no_listener(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Nothing binds a port or prints a banner; the calls are recorded."""
    seen: list[str] = []

    def check(port: int) -> None:
        seen.append(f"check {port}")

    def show(*_a: object, **_k: object) -> None:
        seen.append("banner")

    monkeypatch.setattr(serve, "check_port", check)
    monkeypatch.setattr(banner, "show", show)
    return seen


def test_one_worker_runs_in_this_process(
    monkeypatch: pytest.MonkeyPatch, no_listener: list[str]
) -> None:
    ran: list[tuple[object, ...]] = []

    def run(_target: object, *rest: object, **_kw: object) -> int:
        ran.append(rest)
        return 0

    monkeypatch.setattr(wsgiworker, "run", run)
    assert serve.run(served_args(PLAIN, port=9101), reload=False) == 0
    assert ran and ran[0][1] == 9101
    assert no_listener == ["check 9101", "banner"]


def test_several_workers_run_under_a_supervisor(
    monkeypatch: pytest.MonkeyPatch, no_listener: list[str]
) -> None:
    built: dict[str, object] = {}

    class FakeSupervisor:
        def __init__(
            self,
            target: str,
            listener: object,
            workers: int,
            grace: float,
            **kw: object,
        ):
            built.update(target=target, workers=workers, grace=grace, **kw)

        def run(self) -> int:
            return 0

    monkeypatch.setattr(supervisor, "Supervisor", FakeSupervisor)
    monkeypatch.setattr(serve, "open_listener", lambda *_a: object())
    assert serve.run(served_args(PLAIN, port=9102, workers=3), reload=False) == 0
    assert built["workers"] == 3
    assert built["interface"] == "wsgi"


def test_dev_always_uses_the_supervisor_with_one_worker(
    monkeypatch: pytest.MonkeyPatch, no_listener: list[str]
) -> None:
    built: dict[str, object] = {}

    class FakeSupervisor:
        def __init__(
            self,
            target: str,
            listener: object,
            workers: int,
            grace: float,
            **kw: object,
        ):
            built.update(workers=workers, reload=kw["reload"])

        def run(self) -> int:
            return 0

    monkeypatch.setattr(supervisor, "Supervisor", FakeSupervisor)
    monkeypatch.setattr(serve, "open_listener", lambda *_a: object())
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    assert serve.run(served_args(PLAIN, port=9103, workers=4), reload=True) == 0
    assert built == {"workers": 1, "reload": True}


def test_an_unsupported_setting_exits_2_before_listening(
    capsys: pytest.CaptureFixture[str], no_listener: list[str]
) -> None:
    status = serve.run_wsgi_kiso(
        served_args(PLAIN),
        False,
        load_wsgi_target(PLAIN),
        ServerSettings(trusted_proxies=("10.0.0.0/8",)),
    )
    assert status == 2
    assert "trusted_proxies" in capsys.readouterr().err
    assert no_listener == []


def test_a_port_in_use_is_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def busy(port: int) -> None:
        raise ListenError("address already in use")

    monkeypatch.setattr(serve, "check_port", busy)
    assert serve.run(served_args(PLAIN, port=9104), reload=False) == 2
    assert "cannot listen on" in capsys.readouterr().err


def test_granian_gets_the_wsgi_interface(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        serve,
        "run_granian",
        lambda _a, _r, _s, interface="asgi": seen.update(i=interface) or 0,
    )
    assert serve.run(served_args(PLAIN, server="granian"), reload=False) == 0
    assert seen == {"i": "wsgi"}


def test_a_hinoki_app_with_a_wsgi_setting_is_refused_as_asgi(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    app = Hinoki(settings=AppSettings(server=ServerSettings(interface="wsgi")))
    monkeypatch.setattr(serve, "built_app", lambda _target: app)
    assert serve.run_asgi(served_args(HINOKI, interface=None), reload=False) == 2
    assert "server.interface is wsgi" in capsys.readouterr().err


class FakeServer:
    port = 4321

    def __init__(self, aborted: int = 0) -> None:
        self.aborted = aborted
        self.stopped = False

    def shutdown(self) -> int:
        self.stopped = True
        return self.aborted


def test_a_plain_callable_has_the_default_limits() -> None:
    assert wsgiworker.app_settings(load_wsgi_target(PLAIN)) == AppSettings()
    adapter = WSGIAdapter(
        Hinoki(settings=AppSettings(execution=ExecutionSettings(sync_workers=3)))
    )
    assert wsgiworker.app_settings(adapter).execution.sync_workers == 3


def test_the_worker_serves_until_stopped_then_drains(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    server = FakeServer(aborted=2)
    monkeypatch.setattr(core, "configure_runtime", lambda _threads: None)
    monkeypatch.setattr(wsgiworker, "start_server", lambda *_a, **_k: server)
    monkeypatch.setattr(wsgiworker, "wait_for_stop", lambda _parent: None)
    assert (
        wsgiworker.run(load_wsgi_target(PLAIN), "127.0.0.1", 0, ServerSettings()) == 0
    )
    assert server.stopped
    assert "listening on http://127.0.0.1:4321" in capsys.readouterr().out


def test_an_adapter_is_started_before_listening_and_closed_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    adapter = WSGIAdapter(Hinoki())
    monkeypatch.setattr(adapter, "start", lambda: events.append("start"))
    monkeypatch.setattr(adapter, "close", lambda: events.append("close"))

    def start_server(*_a: object, **_k: object) -> FakeServer:
        events.append("listen")
        return FakeServer()

    monkeypatch.setattr(core, "configure_runtime", lambda _threads: None)
    monkeypatch.setattr(wsgiworker, "start_server", start_server)
    monkeypatch.setattr(wsgiworker, "wait_for_stop", lambda _parent: None)
    wsgiworker.run(adapter, "::1", 0, ServerSettings(), parent=1)
    assert events == ["start", "listen", "close"]


def test_wait_for_stop_returns_on_sigterm() -> None:
    timer = threading.Timer(0.2, os.kill, (os.getpid(), signal.SIGTERM))
    previous = signal.getsignal(signal.SIGTERM)
    timer.start()
    try:
        wsgiworker.wait_for_stop(None)
    finally:
        timer.cancel()
        signal.signal(signal.SIGTERM, previous)
        signal.signal(signal.SIGINT, signal.default_int_handler)


def test_wait_for_stop_returns_when_the_supervisor_is_gone() -> None:
    previous = signal.getsignal(signal.SIGTERM)
    try:
        wsgiworker.wait_for_stop(os.getppid() + 1)  # not our parent
    finally:
        signal.signal(signal.SIGTERM, previous)
        signal.signal(signal.SIGINT, signal.default_int_handler)
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
