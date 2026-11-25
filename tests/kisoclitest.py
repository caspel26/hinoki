"""``hinoki serve|dev --server``: choosing the server, refusing options it cannot
honour, and the listening socket the Rust server is given."""

import argparse
import asyncio
import builtins
import importlib.util
import os
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from granian.constants import HTTPModes, SSLProtocols

import hinoki.serve as serve_module
from hinoki import Hinoki, core
from hinoki.runtime.kiso import KisoWorker
from hinoki.runtime.kiso import main as kiso_main
from hinoki.runtime.listener import (
    ListenError,
    inherited_address,
    open_listener,
    supports_reuse_port,
)
from hinoki.runtime.supervisor import Supervisor, docs_line
from hinoki.serve import add_arguments, choose_server, granian_tls, warn_key_access
from hinoki.settings import (
    MAX_WORKERS,
    AppSettings,
    LifecycleSettings,
    ServerSettings,
    SettingsError,
)
from hinoki.terminal import ANSI, Style

ROOT = Path(__file__).resolve().parents[1]


def parsed(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_arguments(parser.add_subparsers(dest="command"))
    return parser.parse_args(["serve", "tests.kisoserverapp:app", *argv])


def test_the_server_defaults_to_rust_and_the_flag_overrides_the_setting() -> None:
    assert ServerSettings().server == "kiso"
    assert choose_server(parsed(), ServerSettings()) == "kiso"
    assert choose_server(parsed("--server", "granian"), ServerSettings()) == "granian"
    assert choose_server(parsed(), ServerSettings(server="granian")) == "granian"
    assert choose_server(
        parsed("--server", "kiso"), ServerSettings(server="granian")
    ) == ("kiso")
    with pytest.raises(SystemExit):
        parsed("--server", "hyper")


def test_both_servers_accept_the_access_log() -> None:
    assert choose_server(parsed(), ServerSettings(access_log=True)) == "kiso"
    granian = parsed("--server", "granian")
    assert choose_server(granian, ServerSettings(access_log=True)) == "granian"


def test_the_access_log_follows_the_setting_and_otherwise_dev() -> None:
    assert ServerSettings().logs_access(dev=True)
    assert not ServerSettings().logs_access(dev=False)
    assert not ServerSettings(access_log=False).logs_access(dev=True)
    assert ServerSettings(access_log=True).logs_access(dev=False)


@pytest.mark.parametrize(
    "limit",
    [
        {"max_connections": 5},
        {"header_read_timeout": 1.5},
        {"body_read_timeout": 2.0},
        {"write_timeout": 5.0},
    ],
)
def test_granian_refuses_a_rust_only_setting(
    limit: dict[str, float], capsys: pytest.CaptureFixture[str]
) -> None:
    settings = ServerSettings(**limit)  # type: ignore[arg-type]
    assert choose_server(parsed("--server", "granian"), settings) == 2
    assert next(iter(limit)) in capsys.readouterr().err
    assert choose_server(parsed(), settings) == "kiso"
    granian = ServerSettings(server="granian", **limit)  # type: ignore[arg-type]
    assert choose_server(parsed(), granian) == 2


def test_a_missing_rust_server_is_an_error_not_a_fallback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delattr(core, "KisoServer")
    assert choose_server(parsed(), ServerSettings()) == 2
    assert "Kiso is not available" in capsys.readouterr().err
    assert choose_server(parsed("--server", "granian"), ServerSettings()) == "granian"


@pytest.mark.parametrize("entry", ["fe80::1%lo0", "10.0.0.0/8%eth0"])
def test_a_trusted_proxy_with_an_ipv6_zone_id_is_refused(entry: str) -> None:
    with pytest.raises(ValueError, match="zone id"):
        ServerSettings(trusted_proxies=(entry,)).validate()
    with pytest.raises(SettingsError, match="zone id"):
        AppSettings.load(env={"SERVER__TRUSTED_PROXIES": entry})
    ServerSettings(trusted_proxies=("fe80::1", "2001:db8::/32")).validate()


@pytest.mark.parametrize(
    "limit",
    [
        {"max_connections": 0},
        {"max_connections": 10**9},
        {"header_read_timeout": 0.0},
        {"body_read_timeout": -1.0},
        {"body_read_timeout": 10**6},
        {"write_timeout": 0.0},
    ],
)
def test_rust_limits_are_validated(limit: dict[str, float]) -> None:
    with pytest.raises(ValueError, match=next(iter(limit))):
        ServerSettings(**limit).validate()  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0.0])
def test_lifecycle_timeouts_must_be_finite_and_positive(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        LifecycleSettings(shutdown_timeout=value).validate()


def test_tls_files_are_validated() -> None:
    cert = Path(__file__).parent / "tls" / "localhost-cert.pem"
    key = cert.with_name("localhost-key.pem")
    ServerSettings(tls_cert_file=str(cert), tls_key_file=str(key)).validate()
    with pytest.raises(ValueError, match="together"):
        ServerSettings(tls_cert_file=str(cert)).validate()
    with pytest.raises(ValueError, match="tls_key_file"):
        ServerSettings(tls_cert_file=str(cert), tls_key_file="/absent").validate()


def test_http2_needs_tls() -> None:
    cert = Path(__file__).parent / "tls" / "localhost-cert.pem"
    key = cert.with_name("localhost-key.pem")
    assert ServerSettings().http2 is False
    ServerSettings(
        http2=True, tls_cert_file=str(cert), tls_key_file=str(key)
    ).validate()
    with pytest.raises(ValueError, match="http2 needs TLS"):
        ServerSettings(http2=True).validate()
    secure = ServerSettings(http2=True, tls_cert_file=str(cert), tls_key_file=str(key))
    assert secure.conflicts("granian") == secure.conflicts("kiso") == []


def test_the_native_server_refuses_http2_without_tls() -> None:
    fd = detached(socket.SOCK_STREAM, listen=True)
    with pytest.raises(ValueError, match="http2 needs TLS"):
        core.KisoServer.start(native_core(), "", 0, fd=fd, http2=True)
    assert not is_open(fd)


def test_granian_negotiates_http2_only_when_asked() -> None:
    cert = Path(__file__).parent / "tls" / "localhost-cert.pem"
    key = cert.with_name("localhost-key.pem")
    pair = (str(cert), str(key))
    assert (
        granian_tls(ServerSettings(tls_cert_file=pair[0], tls_key_file=pair[1]))["http"]
        == HTTPModes.http1
    )
    assert (
        granian_tls(
            ServerSettings(http2=True, tls_cert_file=pair[0], tls_key_file=pair[1])
        )["http"]
        == HTTPModes.auto
    )
    assert granian_tls(ServerSettings()) == {}
    assert (
        granian_tls(ServerSettings(tls_cert_file=pair[0], tls_key_file=pair[1]))[
            "ssl_protocol_min"
        ]
        == SSLProtocols.tls12
    )


def test_a_world_readable_tls_key_is_warned_about(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = tmp_path / "key.pem"
    key.write_text("x")
    settings = ServerSettings(tls_cert_file=str(key), tls_key_file=str(key))
    key.chmod(0o640)
    warn_key_access(settings)
    assert capsys.readouterr().err == ""
    key.chmod(0o644)
    warn_key_access(settings)
    assert "readable by every local user" in capsys.readouterr().err


def test_a_supervised_worker_ignores_a_repeated_sigterm() -> None:
    worker = KisoWorker(Hinoki(), "127.0.0.1", 0, parent=os.getpid())
    worker._on_signal()
    worker._on_signal()  # would os._exit(1) for an unsupervised worker
    assert worker.stopping.is_set()


def test_the_supervisor_grace_covers_every_stop_step() -> None:
    from hinoki.serve import stop_grace  # noqa: PLC0415

    assert stop_grace(Hinoki()) == 3 * Hinoki().runtime.shutdown_timeout


def test_the_server_group_loads_the_new_fields_from_the_environment() -> None:
    loaded = AppSettings.load(
        env={
            "SERVER__SERVER": "kiso",
            "SERVER__MAX_CONNECTIONS": "100",
            "SERVER__HEADER_READ_TIMEOUT": "2.5",
        }
    ).server
    assert (loaded.server, loaded.max_connections, loaded.header_read_timeout) == (
        "kiso",
        100,
        2.5,
    )
    with pytest.raises(SettingsError, match="must be one of"):
        AppSettings.load(env={"SERVER__SERVER": "hyper"})


def test_the_listener_is_bound_and_listening() -> None:
    listener = open_listener("127.0.0.1", 0, 16)
    try:
        host, port = inherited_address(listener.fileno())
        assert (host, port) == ("127.0.0.1", listener.getsockname()[1])
        assert listener.fileno() >= 0  # inherited_address left it open
        with socket.create_connection((host, port), timeout=2):
            pass
    finally:
        listener.close()


def test_a_taken_port_is_an_error() -> None:
    first = open_listener("127.0.0.1", 0, 16)
    try:
        with pytest.raises(OSError, match="in use"):
            open_listener("127.0.0.1", first.getsockname()[1], 16)
    finally:
        first.close()


def test_an_inherited_descriptor_that_is_not_a_socket_is_refused() -> None:
    reader, writer = os.pipe()
    try:
        with pytest.raises(OSError, match="Socket operation on non-socket"):
            inherited_address(reader)
    finally:
        os.close(reader)
        os.close(writer)


def test_an_inherited_datagram_socket_is_not_a_listener() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as datagram:
        with pytest.raises(OSError, match="not a listening TCP socket"):
            inherited_address(datagram.fileno())
        assert datagram.fileno() >= 0  # the refusal left the descriptor open


async def test_the_native_server_refuses_a_descriptor_that_is_not_a_listener() -> None:
    reader, writer = os.pipe()
    worker = KisoWorker(Hinoki(), "127.0.0.1", 0, listener_fd=reader)
    try:
        with pytest.raises(RuntimeError, match="non-socket"):
            await worker.start()
    finally:
        os.close(writer)  # the native layer closed the read end it was handed


def test_the_supervisor_process_never_starts_the_native_runtime() -> None:
    """Building the application and opening the socket, as ``serve`` does before
    it starts workers, leaves the runtime configurable: it was not created."""
    code = (
        "from hinoki import core\n"
        "from hinoki.runtime.listener import open_listener\n"
        "from hinoki.runtime.supervisor import Supervisor\n"
        "from hinoki.serve import built_app\n"
        "app = built_app('tests.kisoserverapp:app')\n"
        "sock = open_listener('127.0.0.1', 0, 8)\n"
        "Supervisor('tests.kisoserverapp:app', sock, 2, 1.0)\n"
        "core.configure_runtime(2)\n"
        "print('runtime not started')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "runtime not started" in done.stdout


def test_server_settings_default_when_an_app_has_no_settings() -> None:
    app = Hinoki()
    app.settings = object()
    assert serve_module.settings_of(app) == ServerSettings()


def test_built_app_returns_an_app_and_reports_factory_settings_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Hinoki()
    monkeypatch.setattr(serve_module, "load_target", lambda _: (app, False))
    assert serve_module.built_app("app:target") is app
    monkeypatch.setattr(serve_module, "load_target", lambda _: (lambda: app, True))
    assert serve_module.built_app("app:factory") is app

    def invalid_factory() -> Hinoki:
        raise SettingsError(["bad settings"])

    monkeypatch.setattr(serve_module, "load_target", lambda _: (invalid_factory, True))
    with pytest.raises(SystemExit, match="bad settings"):
        serve_module.built_app("app:invalid")


def test_load_target_accepts_apps_and_only_zero_argument_factories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Hinoki()
    monkeypatch.setattr(serve_module, "import_target", lambda _: app)
    assert serve_module.load_target("app:instance") == (app, False)
    monkeypatch.setattr(serve_module, "import_target", lambda _: lambda value: value)
    with pytest.raises(SystemExit, match="neither an application nor a factory"):
        serve_module.load_target("app:requires_argument")
    monkeypatch.setattr(serve_module, "import_target", lambda _: object())
    with pytest.raises(SystemExit, match="neither an application nor a factory"):
        serve_module.load_target("app:not_callable")


def test_load_target_reports_import_and_attribute_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for error in (
        ImportError("missing module"),
        AttributeError("missing name"),
        ValueError("bad target"),
    ):
        monkeypatch.setattr(serve_module, "import_target", Mock(side_effect=error))
        with pytest.raises(SystemExit, match="cannot load app:target"):
            serve_module.load_target("app:target")


@pytest.mark.parametrize("workers", [-1, MAX_WORKERS + 1])
def test_rust_server_refuses_worker_counts_outside_bounds(workers: int) -> None:
    args = argparse.Namespace(workers=workers)
    assert "--workers" in serve_module.rust_refusal(args, reload=False)  # type: ignore[operator]


def test_rust_dev_explains_missing_watchfiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _: None)
    refusal = serve_module.rust_refusal(argparse.Namespace(workers=None), reload=True)
    assert refusal is not None and "watchfiles" in refusal
    assert (
        serve_module.rust_refusal(argparse.Namespace(workers=None), reload=False)
        is None
    )


def test_run_inline_configures_runtime_before_starting_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Hinoki()
    settings = ServerSettings(runtime_threads=3)
    worker = SimpleNamespace(run=lambda: None)
    configure = Mock()
    run = Mock(return_value=0)
    monkeypatch.setattr(core, "configure_runtime", configure)
    monkeypatch.setattr(asyncio, "run", run)
    monkeypatch.setattr("hinoki.runtime.kiso.build_worker", lambda *_: worker)
    assert serve_module.run_inline(app, ("127.0.0.1", 8123), settings) == 0
    configure.assert_called_once_with(3)
    run.assert_called_once_with(None)


def test_run_supervised_opens_listener_and_computes_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = open_listener("127.0.0.1", 0, 8)
    supervisor = Mock()
    supervisor.return_value.run.return_value = 4
    monkeypatch.setattr(serve_module, "open_listener", Mock(return_value=listener))
    monkeypatch.setattr("hinoki.runtime.supervisor.Supervisor", supervisor)
    args = argparse.Namespace(target="app:factory", workers=None, host=None, port=None)
    cert = Path(__file__).parent / "tls" / "localhost-cert.pem"
    key = cert.with_name("localhost-key.pem")
    settings = ServerSettings(tls_cert_file=str(cert), tls_key_file=str(key))
    app = Hinoki(settings=AppSettings(server=settings))
    assert (
        serve_module.run_supervised(args, app, ("127.0.0.1", 8123), settings, False)
        == 4
    )
    supervisor.assert_called_once()
    assert supervisor.call_args.kwargs == {
        "reload": False,
        "scheme": "https",
        "metrics": None,
        "rolling": serve_module.rolls_on_hangup(settings, False),
        "startup_timeout": app.runtime.startup_timeout,
        "reuse_port": False,
    }
    listener.close()


def test_run_supervised_passes_the_applications_docs_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = open_listener("127.0.0.1", 0, 8)
    supervisor = Mock()
    monkeypatch.setattr(serve_module, "open_listener", Mock(return_value=listener))
    monkeypatch.setattr("hinoki.runtime.supervisor.Supervisor", supervisor)
    args = argparse.Namespace(target="app:factory", workers=None)
    app = Hinoki(openapi_url="/openapi.json", docs_url="/docs")
    settings = ServerSettings()
    serve_module.run_supervised(args, app, ("127.0.0.1", 8123), settings, True)
    assert supervisor.return_value.docs == "/docs"
    listener.close()


@pytest.mark.parametrize("colour", [False, True])
def test_the_docs_line_is_coloured_only_where_the_terminal_takes_colour(
    colour: bool,
) -> None:
    style = Style(color=colour, depth="truecolor")
    line = docs_line("http://127.0.0.1:8000/docs", style)
    assert ANSI.sub("", line) == "docs: http://127.0.0.1:8000/docs"
    assert ("\x1b[" in line) is colour


@pytest.mark.parametrize(
    ("docs", "line"),
    [("/docs", "docs: http://127.0.0.1:{port}/docs"), (None, None)],
)
def test_the_supervisor_announces_the_docs_url_after_the_address(
    docs: str | None,
    line: str | None,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = open_listener("127.0.0.1", 0, 4)
    port = listener.getsockname()[1]
    supervisor = Supervisor("app:make", listener, 1, 1.0, reload=True)
    supervisor.docs = docs
    monkeypatch.setattr(signal, "signal", lambda *_: None)
    supervisor.stop.set()
    assert supervisor.run() == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"listening on http://127.0.0.1:{port} (reloading)"
    assert lines[1:] == ([line.format(port=port)] if line else [])


def test_run_rust_chooses_inline_or_supervised_and_refuses_bad_listener(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    app = Hinoki()
    settings = ServerSettings(sighup="stop")
    args = argparse.Namespace(host=None, port=None, workers=1)
    monkeypatch.setattr(serve_module, "check_port", lambda _: None)
    inline = Mock(return_value=0)
    supervised = Mock(return_value=0)
    monkeypatch.setattr(serve_module, "run_inline", inline)
    monkeypatch.setattr(serve_module, "run_supervised", supervised)
    assert serve_module.run_rust(args, False, app, settings) == 0
    inline.assert_called_once()
    args.workers = 2
    assert serve_module.run_rust(args, False, app, settings) == 0
    supervised.assert_called_once()
    monkeypatch.setattr(
        serve_module,
        "check_port",
        Mock(side_effect=ListenError("invalid port")),
    )
    assert serve_module.run_rust(args, False, app, settings) == 2
    assert "cannot listen" in capsys.readouterr().err


def test_run_rust_refuses_unsupported_settings_before_opening_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(serve_module, "check_port", Mock(side_effect=AssertionError))
    args = argparse.Namespace(host=None, port=None, workers=MAX_WORKERS + 1)
    assert serve_module.run_rust(args, False, Hinoki(), ServerSettings()) == 2


def test_run_dispatches_to_rust_or_granian(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Hinoki()
    monkeypatch.setattr(serve_module, "built_app", lambda _: app)
    monkeypatch.setattr(serve_module, "settings_of", lambda _: ServerSettings())
    monkeypatch.setattr(serve_module, "choose_server", lambda *_: "kiso")
    rust = Mock(return_value=0)
    monkeypatch.setattr(serve_module, "run_rust", rust)
    args = argparse.Namespace(target="app:target")
    assert serve_module.run(args, False) == 0
    rust.assert_called_once_with(args, False, app, ServerSettings())
    monkeypatch.setattr(serve_module, "choose_server", lambda *_: "granian")
    granian = Mock(return_value=0)
    monkeypatch.setattr(serve_module, "run_granian", granian)
    assert serve_module.run(args, True) == 0
    granian.assert_called_once_with(args, True, ServerSettings())


def test_run_returns_server_selection_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(serve_module, "built_app", lambda _: Hinoki())
    monkeypatch.setattr(serve_module, "settings_of", lambda _: ServerSettings())
    monkeypatch.setattr(serve_module, "choose_server", lambda *_: 2)
    assert serve_module.run(argparse.Namespace(target="app:target"), False) == 2


def test_serve_entry_point_selects_production_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = argparse.Namespace(target="app:target")
    run = Mock(return_value=0)
    monkeypatch.setattr(serve_module, "run", run)
    assert serve_module.run_serve(args) == 0
    run.assert_called_once_with(args, reload=False)


def test_granian_errors_when_optional_dependency_is_absent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real_import = builtins.__import__

    def import_without_granian(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "granian":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_granian)
    args = argparse.Namespace(target="app:target")
    assert serve_module.run_granian(args, False, ServerSettings()) == 2
    assert "Granian is not installed" in capsys.readouterr().err


def test_granian_refuses_reload_without_watchfiles(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _: None)
    args = argparse.Namespace(target="app:target")
    assert serve_module.run_granian(args, True, ServerSettings()) == 2
    assert "watchfiles" in capsys.readouterr().err


def test_granian_starts_with_settings_and_factory_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    server = Mock()
    monkeypatch.setattr("granian.Granian", server)
    monkeypatch.setattr(serve_module, "load_target", lambda _: (object(), True))
    args = argparse.Namespace(target="app:factory", host=None, port=None, workers=None)
    assert serve_module.run_granian(args, False, ServerSettings()) == 0
    assert server.call_args.kwargs["factory"] is True
    server.return_value.serve.assert_called_once()


def is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def native_core() -> core.ServerCore:
    return KisoWorker(Hinoki(), "127.0.0.1", 0).core


def detached(kind: int, *, listen: bool) -> int:
    sock = socket.socket(socket.AF_INET, kind)
    if kind == socket.SOCK_STREAM:
        sock.bind(("127.0.0.1", 0))
        if listen:
            sock.listen(4)
    return sock.detach()


def test_a_descriptor_that_is_not_an_ip_stream_socket_is_refused_and_closed() -> None:
    for fd in (
        detached(socket.SOCK_DGRAM, listen=False),
        os.open(os.devnull, os.O_RDONLY),  # a file some other code opened
    ):
        with pytest.raises((RuntimeError, ValueError)):
            core.KisoServer.start(native_core(), "", 0, fd=fd)
        assert not is_open(fd)  # taken by the call, so closed with the error


@pytest.mark.skipif(sys.platform != "linux", reason="SO_ACCEPTCONN is read on Linux")
def test_a_stream_socket_that_is_not_listening_is_refused_and_closed() -> None:
    fd = detached(socket.SOCK_STREAM, listen=False)
    with pytest.raises(ValueError, match="not a listening socket"):
        core.KisoServer.start(native_core(), "", 0, fd=fd)
    assert not is_open(fd)


def test_a_valid_listener_is_closed_when_the_start_is_refused_for_its_limits() -> None:
    fd = detached(socket.SOCK_STREAM, listen=True)
    with pytest.raises(ValueError, match="max_connections"):
        core.KisoServer.start(native_core(), "", 0, max_connections=0, fd=fd)
    assert not is_open(fd)  # one rule: a descriptor passed in is always consumed
    with pytest.raises(ValueError, match="descriptor"):
        core.KisoServer.start(native_core(), "", 0, fd=-1)


def test_the_listener_helper_refuses_a_socket_that_is_not_listening() -> None:
    if sys.platform != "linux":
        pytest.skip("macOS cannot tell a listening socket from a bound one")
    fd = detached(socket.SOCK_STREAM, listen=False)
    try:
        with pytest.raises(OSError, match="not a listening TCP socket"):
            inherited_address(fd)
    finally:
        os.close(fd)


@pytest.mark.parametrize("reload", [False, True])
def test_a_signal_before_the_workers_start_stops_cleanly(
    reload: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    listener = open_listener("127.0.0.1", 0, 4)
    supervisor = Supervisor("tests.kisoserverapp:app", listener, 2, 1.0, reload=reload)
    monkeypatch.setattr(signal, "signal", lambda *_: None)  # not the test's own
    supervisor.stop.set()  # the signal came during the first poll
    assert supervisor.run() == 0
    assert supervisor.processes == []
    assert listener.fileno() == -1  # closed


@pytest.mark.parametrize("port", [70000, 65536, -1])
def test_a_port_out_of_range_is_refused_even_if_the_resolver_wraps_it(
    port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = socket.getaddrinfo  # glibc wraps the port to 16 bits instead of failing
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda h, p, **k: real(h, p & 0xFFFF, **k)
    )
    with pytest.raises(ListenError, match="invalid port"):
        open_listener("127.0.0.1", port, 4)


@pytest.mark.parametrize(
    "error", [OverflowError("x"), ValueError("y"), UnicodeError("z")]
)
def test_resolver_and_bind_errors_that_are_not_oserrors_become_listen_errors(
    error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    def resolver(*_: object, **__: object) -> list[object]:
        raise error

    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    with pytest.raises(ListenError, match="invalid address"):
        open_listener("127.0.0.1", 8000, 4)

    def bind(self: socket.socket, address: object) -> None:
        raise error

    monkeypatch.undo()
    monkeypatch.setattr(socket.socket, "bind", bind)
    with pytest.raises(ListenError, match="invalid address"):
        open_listener("127.0.0.1", 8000, 4)


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_runtime_timeouts_must_be_finite(value: float) -> None:
    from hinoki import RuntimeConfig  # noqa: PLC0415

    with pytest.raises(ValueError, match="finite"):
        RuntimeConfig(shutdown_timeout=value)


class FakeWorkerProcess:
    def __init__(self, code: int | None = None, *, times_out: bool = False) -> None:
        self.code = code
        self.times_out = times_out
        self.signals: list[signal.Signals] = []
        self.killed = False
        self.waits = 0
        self.pid = id(self)

    def poll(self) -> int | None:
        return self.code

    def send_signal(self, number: signal.Signals) -> None:
        self.signals.append(number)

    def wait(self, timeout: float | None = None) -> int:
        self.waits += 1
        if self.times_out and self.waits == 1:
            raise subprocess.TimeoutExpired("worker", timeout or 0)
        return self.code or 0

    def kill(self) -> None:
        self.killed = True
        self.code = -signal.SIGKILL


def test_supervisor_builds_and_spawns_worker_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = open_listener("127.0.0.1", 0, 8)
    supervisor = Supervisor("app:make", listener, 1, 2.0)
    popen = Mock(return_value=FakeWorkerProcess())
    monkeypatch.setattr("hinoki.runtime.supervisor.subprocess.Popen", popen)
    command = supervisor.command()
    assert command[:4] == [
        sys.executable,
        "-m",
        "hinoki.runtime.kiso",
        "app:make",
    ]
    assert command[4:] == ["--fd", str(listener.fileno()), "--parent", str(os.getpid())]
    supervisor.spawn()
    assert popen.call_args.kwargs["pass_fds"] == (listener.fileno(),)
    assert (
        Supervisor("app:make", listener, 1, 2.0, reload=True).command()[-1] == "--dev"
    )
    listener.close()


def test_a_worker_starts_with_sigint_blocked_and_the_supervisor_mask_is_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = open_listener("127.0.0.1", 0, 8)
    masks: list[set[int | signal.Signals]] = []

    def popen(*_: object, **__: object) -> FakeWorkerProcess:
        masks.append(signal.pthread_sigmask(signal.SIG_BLOCK, []))
        return FakeWorkerProcess()

    monkeypatch.setattr("hinoki.runtime.supervisor.subprocess.Popen", popen)
    before = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    Supervisor("app:make", listener, 1, 2.0).spawn()
    assert signal.SIGINT in masks[0]  # inherited across exec by the worker
    assert signal.pthread_sigmask(signal.SIG_BLOCK, []) == before
    listener.close()


def test_a_supervised_worker_takes_interrupts_back_before_loading_the_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hinoki.runtime import kiso  # noqa: PLC0415

    class Loaded(Exception):
        pass

    masks: list[set[int | signal.Signals]] = []
    handlers: list[object] = []

    def load(_: str) -> None:
        masks.append(signal.pthread_sigmask(signal.SIG_BLOCK, []))
        handlers.append(signal.getsignal(signal.SIGINT))
        raise Loaded

    monkeypatch.setattr(kiso, "load_target", load)
    previous = signal.getsignal(signal.SIGINT)
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    try:
        # A Ctrl-C while blocked, pending on this thread alone: a process-wide
        # one could land on another thread of the test run.
        signal.pthread_kill(threading.get_ident(), signal.SIGINT)
        with pytest.raises(Loaded):  # not KeyboardInterrupt
            kiso.main(["app:make", "--parent", str(os.getppid())])
    finally:
        signal.signal(signal.SIGINT, lambda *_: None)  # drain a still-pending one
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        signal.signal(signal.SIGINT, previous)
    assert signal.SIGINT not in masks[0]  # threads and children start unblocked
    assert callable(handlers[0])  # a Python handler, which exec resets, not SIG_IGN
    assert handlers[0] is not signal.default_int_handler


def test_supervisor_second_signal_kills_and_terminate_skips_exited_workers() -> None:
    listener = open_listener("127.0.0.1", 0, 8)
    supervisor = Supervisor("app:make", listener, 1, 2.0)
    alive = FakeWorkerProcess()
    stopped = FakeWorkerProcess(code=0)
    supervisor.processes = [alive, stopped]  # type: ignore[list-item]
    supervisor.on_signal(signal.SIGTERM, None)
    assert supervisor.stop.is_set()
    assert alive.signals == []
    supervisor.on_signal(signal.SIGTERM, None)
    assert alive.signals == [signal.SIGKILL]
    assert stopped.signals == []
    listener.close()


def test_supervisor_wait_all_kills_only_workers_past_the_deadline() -> None:
    listener = open_listener("127.0.0.1", 0, 8)
    supervisor = Supervisor("app:make", listener, 1, 2.0)
    process = FakeWorkerProcess(times_out=True)
    supervisor.processes = [process]  # type: ignore[list-item]
    supervisor.wait_all()
    assert process.killed
    assert process.waits == 2
    supervisor.stop_workers()
    assert supervisor.processes == []
    listener.close()


@pytest.mark.parametrize(("code", "expected"), [(0, 1), (-9, 1), (7, 7), (None, None)])
def test_supervisor_reports_worker_exit_status(
    code: int | None, expected: int | None
) -> None:
    listener = open_listener("127.0.0.1", 0, 8)
    supervisor = Supervisor("app:make", listener, 1, 2.0)
    supervisor.processes = [FakeWorkerProcess(code=code)]  # type: ignore[list-item]
    assert supervisor.failed() == expected
    listener.close()


def test_supervisor_ticks_end_when_stopping() -> None:
    listener = open_listener("127.0.0.1", 0, 8)
    supervisor = Supervisor("app:make", listener, 1, 2.0)
    supervisor.stop.set()
    assert list(supervisor.ticks()) == []
    listener.close()


def test_supervisor_ticks_yield_until_stopping(monkeypatch: pytest.MonkeyPatch) -> None:
    listener = open_listener("127.0.0.1", 0, 8)
    supervisor = Supervisor("app:make", listener, 1, 2.0)
    waits = iter([False, True])
    monkeypatch.setattr(supervisor.stop, "wait", lambda _: next(waits))
    assert list(supervisor.ticks()) == [set()]
    listener.close()


def test_supervisor_file_watch_uses_python_project_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from watchfiles import PythonFilter  # noqa: PLC0415

    listener = open_listener("127.0.0.1", 0, 8)
    supervisor = Supervisor("app:make", listener, 1, 2.0, reload=True)
    watch = Mock(return_value=iter([{(None, "app.py")}]))
    monkeypatch.setattr("watchfiles.watch", watch)
    assert list(supervisor.changes()) == [{(None, "app.py")}]
    options = watch.call_args.kwargs
    assert isinstance(options["watch_filter"], PythonFilter)
    assert options["stop_event"] is supervisor.stop
    assert options["yield_on_timeout"] is True
    listener.close()


def test_supervisor_supervise_stops_on_a_failed_worker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    listener = open_listener("127.0.0.1", 0, 8)
    supervisor = Supervisor("app:make", listener, 1, 2.0)
    supervisor.processes = [FakeWorkerProcess(code=9)]  # type: ignore[list-item]
    assert supervisor.supervise(iter([set()])) == 9
    assert "worker exited (9)" in capsys.readouterr().err
    listener.close()


def test_supervisor_supervise_returns_success_when_workers_keep_running() -> None:
    listener = open_listener("127.0.0.1", 0, 8)
    supervisor = Supervisor("app:make", listener, 1, 2.0)
    supervisor.processes = [FakeWorkerProcess()]  # type: ignore[list-item]
    assert supervisor.supervise(iter([set()])) == 0
    listener.close()


def test_supervisor_watch_restarts_changed_files_and_waits_after_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    listener = open_listener("127.0.0.1", 0, 8)
    supervisor = Supervisor("app:make", listener, 1, 2.0, reload=True)
    original = FakeWorkerProcess(code=4)
    restarted = FakeWorkerProcess()
    supervisor.processes = [original]  # type: ignore[list-item]
    spawn = Mock(return_value=restarted)
    stop_workers = Mock(side_effect=lambda: setattr(supervisor, "processes", []))
    monkeypatch.setattr(supervisor, "spawn", spawn)
    monkeypatch.setattr(supervisor, "stop_workers", stop_workers)
    events = iter([set(), {(None, "z.py"), (None, "a.py")}, set()])
    assert supervisor.watch(events) == 0
    assert stop_workers.call_count == 1
    assert spawn.call_count == 1
    output = capsys.readouterr().out
    assert "worker exited; waiting for changes" in output
    assert "reloading: a.py, z.py changed" in output
    listener.close()


@pytest.mark.parametrize("reload", [False, True])
def test_supervisor_run_owns_worker_and_listener_until_exit(
    reload: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    listener = open_listener("127.0.0.1", 0, 8)
    supervisor = Supervisor("app:make", listener, 1, 2.0, reload=reload)
    process = FakeWorkerProcess()
    signals: list[tuple[signal.Signals, object]] = []
    monkeypatch.setattr(
        signal,
        "signal",
        lambda number, handler: signals.append((number, handler)),
    )
    monkeypatch.setattr(supervisor, "changes", lambda: iter([set()]))
    monkeypatch.setattr(supervisor, "ticks", lambda: iter([set()]))
    monkeypatch.setattr(supervisor, "spawn", lambda _slot: process)
    monkeypatch.setattr(supervisor, "watch", lambda _events: 0)
    monkeypatch.setattr(supervisor, "supervise", lambda _events: 0)
    assert supervisor.run() == 0
    assert listener.fileno() == -1
    assert process.signals == [signal.SIGTERM]
    assert supervisor.processes == []
    assert len(signals) == 6
    assert all(handler is signal.SIG_IGN for _, handler in signals[-3:])


def test_reuse_port_setting_is_kiso_only_and_loads_from_env(
    capsys: pytest.CaptureFixture[str],
) -> None:
    loaded = AppSettings.load(env={"SERVER__REUSE_PORT": "false"}).server
    assert loaded.reuse_port is False
    assert choose_server(parsed("--server", "granian"), loaded) == 2
    assert "reuse_port" in capsys.readouterr().err
    assert choose_server(parsed("--server", "kiso"), loaded) == "kiso"


@pytest.mark.parametrize(
    ("platform_ok", "setting", "workers", "port", "reload", "expected"),
    [
        (True, None, 4, 8000, False, True),
        (True, True, 4, 8000, False, True),
        (True, False, 4, 8000, False, False),
        (True, None, 1, 8000, False, False),
        (True, None, 4, 0, False, False),
        (True, None, 4, 8000, True, False),
        (False, None, 4, 8000, False, False),
        (False, True, 4, 8000, False, False),
    ],
)
def test_use_reuse_port_policy(
    platform_ok: bool,
    setting: bool | None,
    workers: int,
    port: int,
    reload: bool,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(serve_module, "supports_reuse_port", lambda: platform_ok)
    settings = ServerSettings(reuse_port=setting)
    assert serve_module.use_reuse_port(settings, workers, port, reload) is expected


def test_open_listener_reuse_port_platform_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert supports_reuse_port() is (
        sys.platform == "linux" and hasattr(socket, "SO_REUSEPORT")
    )
    monkeypatch.setattr("hinoki.runtime.listener.supports_reuse_port", lambda: False)
    with pytest.raises(ListenError, match="only supported on Linux"):
        open_listener("127.0.0.1", 0, 8, reuse_port=True)


def test_open_listener_sets_so_reuseport_when_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not hasattr(socket, "SO_REUSEPORT"):
        pytest.skip("platform lacks SO_REUSEPORT constant")
    monkeypatch.setattr("hinoki.runtime.listener.supports_reuse_port", lambda: True)
    sock = open_listener("127.0.0.1", 0, 8, reuse_port=True)
    try:
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT) != 0
    finally:
        sock.close()


def test_supervisor_reuse_port_closes_probe_and_passes_host_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = open_listener("127.0.0.1", 0, 8)
    port = listener.getsockname()[1]
    supervisor = Supervisor("app:make", listener, 2, 2.0, rolling=True, reuse_port=True)
    assert supervisor.reuse_port is True
    dev_supervisor = Supervisor(
        "app:make", listener, 1, 2.0, reload=True, reuse_port=True
    )
    assert dev_supervisor.reuse_port is False

    spawned_cmds: list[list[str]] = []
    spawned_fds: list[tuple[int, ...]] = []
    listener_closed_at_spawn: list[bool] = []

    def fake_popen(cmd: list[str], pass_fds: tuple[int, ...] = ()) -> FakeWorkerProcess:
        spawned_cmds.append(cmd)
        spawned_fds.append(pass_fds)
        listener_closed_at_spawn.append(listener.fileno() == -1)
        return FakeWorkerProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(signal, "signal", lambda *_: None)
    monkeypatch.setattr(supervisor, "ticks", lambda: iter([set()]))
    monkeypatch.setattr(supervisor, "supervise", lambda _events: 0)
    assert supervisor.run() == 0
    assert listener_closed_at_spawn == [True, True]
    assert spawned_fds == [(), ()]
    for cmd in spawned_cmds:
        assert "--fd" not in cmd
        assert "--reuse-port" in cmd
        assert "--host" in cmd and "127.0.0.1" in cmd
        assert "--port" in cmd and str(port) in cmd


def test_kiso_worker_cli_refuses_fd_and_reuse_port_together() -> None:
    with pytest.raises(SystemExit) as exc:
        kiso_main(["tests.kisoserverapp:app", "--fd", "3", "--reuse-port"])
    assert exc.value.code == 2


def test_native_server_refuses_fd_and_reuse_port_together_and_closes_fd() -> None:
    fd = detached(socket.SOCK_STREAM, listen=True)
    with pytest.raises(ValueError, match="fd and reuse_port"):
        core.KisoServer.start(native_core(), "127.0.0.1", 0, fd=fd, reuse_port=True)
    assert not is_open(fd)


@pytest.mark.skipif(sys.platform == "linux", reason="non-Linux refusal test")
def test_native_server_refuses_reuse_port_on_non_linux() -> None:
    with pytest.raises(ValueError, match="only supported on Linux"):
        core.KisoServer.start(native_core(), "127.0.0.1", 0, reuse_port=True)
