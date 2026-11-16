"""``hinoki serve|dev --server``: choosing the server, refusing options it cannot
honour, and the listening socket the Rust server is given."""

import argparse
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from hinoki import Hinoki, core
from hinoki.runtime.listener import inherited_address, open_listener
from hinoki.runtime.rustserver import RustWorker
from hinoki.serve import add_arguments, choose_server
from hinoki.settings import AppSettings, ServerSettings, SettingsError

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
    [{"max_connections": 5}, {"header_read_timeout": 1.5}, {"body_read_timeout": 2.0}],
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
    ],
)
def test_rust_limits_are_validated(limit: dict[str, float]) -> None:
    with pytest.raises(ValueError, match=next(iter(limit))):
        ServerSettings(**limit).validate()  # type: ignore[arg-type]


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
