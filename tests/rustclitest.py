"""``hinoki serve|dev --server``: choosing the server, refusing options it cannot
honour, and the listening socket the Rust server is given."""

import argparse
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from hinoki import Hinoki, core
from hinoki.runtime.listener import ListenError, inherited_address, open_listener
from hinoki.runtime.rustserver import RustWorker
from hinoki.runtime.supervisor import Supervisor
from hinoki.serve import add_arguments, choose_server
from hinoki.settings import (
    AppSettings,
    LifecycleSettings,
    ServerSettings,
    SettingsError,
)

ROOT = Path(__file__).resolve().parents[1]


def parsed(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_arguments(parser.add_subparsers(dest="command"))
    return parser.parse_args(["serve", "tests.rustserverapp:app", *argv])


def test_the_server_defaults_to_granian_and_the_flag_overrides_the_setting() -> None:
    assert ServerSettings().server == "granian"
    assert choose_server(parsed(), ServerSettings()) == "granian"
    assert choose_server(parsed("--server", "rust"), ServerSettings()) == "rust"
    assert choose_server(parsed(), ServerSettings(server="rust")) == "rust"
    assert choose_server(
        parsed("--server", "granian"), ServerSettings(server="rust")
    ) == ("granian")
    with pytest.raises(SystemExit):
        parsed("--server", "hyper")


def test_the_rust_server_refuses_a_granian_only_setting(
    capsys: pytest.CaptureFixture[str],
) -> None:
    refused = choose_server(parsed("--server", "rust"), ServerSettings(access_log=True))
    assert refused == 2
    error = capsys.readouterr().err
    assert "server.access_log is not supported by the rust server" in error
    assert choose_server(parsed(), ServerSettings(access_log=True)) == "granian"


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
    assert choose_server(parsed(), settings) == 2
    assert next(iter(limit)) in capsys.readouterr().err
    assert choose_server(parsed("--server", "rust"), settings) == "rust"
    assert choose_server(parsed(), ServerSettings(server="rust", **limit)) == "rust"  # type: ignore[arg-type]


def test_a_missing_rust_server_is_an_error_not_a_fallback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delattr(core, "RustServer")
    assert choose_server(parsed("--server", "rust"), ServerSettings()) == 2
    assert "Rust server is not available" in capsys.readouterr().err
    assert choose_server(parsed(), ServerSettings()) == "granian"


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


def test_a_supervised_worker_ignores_a_repeated_sigterm() -> None:
    worker = RustWorker(Hinoki(), "127.0.0.1", 0, parent=os.getpid())
    worker._on_signal()
    worker._on_signal()  # would os._exit(1) for an unsupervised worker
    assert worker.stopping.is_set()


def test_the_supervisor_grace_covers_every_stop_step() -> None:
    from hinoki.serve import stop_grace  # noqa: PLC0415

    assert stop_grace(Hinoki()) == 3 * Hinoki().runtime.shutdown_timeout


def test_the_server_group_loads_the_new_fields_from_the_environment() -> None:
    loaded = AppSettings.load(
        env={
            "SERVER__SERVER": "rust",
            "SERVER__MAX_CONNECTIONS": "100",
            "SERVER__HEADER_READ_TIMEOUT": "2.5",
        }
    ).server
    assert (loaded.server, loaded.max_connections, loaded.header_read_timeout) == (
        "rust",
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


async def test_the_native_server_refuses_a_descriptor_that_is_not_a_listener() -> None:
    reader, writer = os.pipe()
    worker = RustWorker(Hinoki(), "127.0.0.1", 0, listener_fd=reader)
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
        "app = built_app('tests.rustserverapp:app')\n"
        "sock = open_listener('127.0.0.1', 0, 8)\n"
        "Supervisor('tests.rustserverapp:app', sock, 2, 1.0)\n"
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


def is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def native_core() -> core.ServerCore:
    return RustWorker(Hinoki(), "127.0.0.1", 0).core


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
            core.RustServer.start(native_core(), "", 0, fd=fd)
        assert not is_open(fd)  # taken by the call, so closed with the error


@pytest.mark.skipif(sys.platform != "linux", reason="SO_ACCEPTCONN is read on Linux")
def test_a_stream_socket_that_is_not_listening_is_refused_and_closed() -> None:
    fd = detached(socket.SOCK_STREAM, listen=False)
    with pytest.raises(ValueError, match="not a listening socket"):
        core.RustServer.start(native_core(), "", 0, fd=fd)
    assert not is_open(fd)


def test_a_valid_listener_is_closed_when_the_start_is_refused_for_its_limits() -> None:
    fd = detached(socket.SOCK_STREAM, listen=True)
    with pytest.raises(ValueError, match="max_connections"):
        core.RustServer.start(native_core(), "", 0, max_connections=0, fd=fd)
    assert not is_open(fd)  # one rule: a descriptor passed in is always consumed
    with pytest.raises(ValueError, match="descriptor"):
        core.RustServer.start(native_core(), "", 0, fd=-1)


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
    supervisor = Supervisor("tests.rustserverapp:app", listener, 2, 1.0, reload=reload)
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
