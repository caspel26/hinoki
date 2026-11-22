"""Prometheus metrics: the settings group, and the Rust server's dedicated
listener over real loopback sockets, alone and with several workers."""

import argparse
import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

from hinoki import Hinoki, events, http
from hinoki.runtime.kiso import KisoWorker
from hinoki.runtime.listener import open_listener
from hinoki.runtime.supervisor import Supervisor
from hinoki.serve import choose_server
from hinoki.settings import (
    AppSettings,
    EventsSettings,
    MetricsSettings,
    Secret,
    ServerSettings,
)

ROOT = Path(__file__).resolve().parent.parent


@http.get("/items/{item_id}")
async def item(item_id: int) -> dict[str, int]:
    return {"id": item_id}


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def fetch(port: int, target: str, headers: str = "") -> str:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    request = f"GET {target} HTTP/1.1\r\nhost: t\r\n{headers}connection: close\r\n\r\n"
    writer.write(request.encode())
    raw = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    return raw.decode("latin-1")


def test_metrics_are_off_unless_listen_is_set() -> None:
    assert MetricsSettings().native_options(version="x") is None
    assert AppSettings().metrics.address() is None


def test_native_options_carry_the_settings_and_the_inherited_listener() -> None:
    settings = MetricsSettings(listen="[::1]:9100", allow=("10.0.0.0/8",))
    options = settings.native_options(version="1.2", worker=3, directory="/d")
    assert options is not None
    assert (options["host"], options["port"]) == ("::1", 9100)
    assert options["worker"] == 3
    assert options["dir"] == "/d"
    assert settings.native_options(version="1.2", fd=7) is not None
    inherited = settings.native_options(version="1.2", fd=7)
    assert inherited is not None
    assert inherited["fd"] == 7
    assert "host" not in inherited


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"listen": "9100"}, "host:port"),
        ({"listen": "127.0.0.1:0"}, "host:port"),
        ({"listen": "0.0.0.0:9100"}, "not on the loopback"),
        ({"listen": "metrics.internal:9100"}, "not on the loopback"),
        ({"path": "metrics"}, "path"),
        ({"allow": ("not-an-address",)}, "allow"),
        ({"buckets": ()}, "buckets"),
        ({"buckets": (0.5, 0.1)}, "buckets"),
        ({"buckets": (0.1, 0.1)}, "buckets"),
        ({"buckets": (float("inf"),)}, "buckets"),
        ({"buckets": tuple(range(1, 40))}, "buckets"),
        ({"snapshot_interval": 0.0}, "snapshot_interval"),
    ],
)
def test_invalid_metrics_settings_are_refused(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        MetricsSettings(**overrides).validate()  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"listen": "127.0.0.1:9100"},
        {"listen": "[::1]:9100"},
        {"listen": "localhost:9100"},
        {"listen": "0.0.0.0:9100", "allow": ("10.0.0.0/8",)},
        {"listen": "0.0.0.0:9100", "token": Secret("s3cret-token")},
    ],
)
def test_a_listener_off_the_loopback_needs_allow_or_token(
    overrides: dict[str, object],
) -> None:
    MetricsSettings(**overrides).validate()  # type: ignore[arg-type]


@pytest.mark.parametrize("workers", [1, 2])
def test_only_several_workers_share_a_snapshot_directory(workers: int) -> None:
    main, scrape = socket.socket(), socket.socket()
    supervisor = Supervisor("app:make", main, workers, 1.0, metrics=scrape)
    try:
        command = supervisor.command(1)
        assert ("--metrics-dir" in command) == (workers > 1)
        assert command[command.index("--metrics-fd") + 1] == str(scrape.fileno())
        assert supervisor.metrics_dir is None or Path(supervisor.metrics_dir).is_dir()
    finally:
        supervisor.discard_snapshots()
        main.close()
        scrape.close()
    assert supervisor.metrics_dir is None or not Path(supervisor.metrics_dir).exists()


def test_granian_refuses_metrics() -> None:
    args = argparse.Namespace(server="granian")
    assert choose_server(args, ServerSettings(), metrics=True) == 2
    assert (
        choose_server(argparse.Namespace(server="kiso"), ServerSettings(), metrics=True)
        == "kiso"
    )


async def test_a_worker_counts_requests_by_route_pattern() -> None:
    app = Hinoki()
    app.include(item)
    listener = open_listener("127.0.0.1", 0, 16)
    metrics_port = listener.getsockname()[1]
    options = MetricsSettings(listen=f"127.0.0.1:{metrics_port}").native_options(
        version="test", fd=listener.detach()
    )
    worker = KisoWorker(app, "127.0.0.1", 0, metrics=options)
    port = await worker.start()
    try:
        await fetch(port, "/items/1")
        await fetch(port, "/items/2")
        await fetch(port, "/absent")
        page = await fetch(metrics_port, "/metrics")
        assert page.startswith("HTTP/1.1 200")
        assert (
            'route="/items/{item_id}",method="GET",status="2xx",worker="0"} 2' in page
        )
        assert 'route="",method="GET",status="4xx",worker="0"} 1' in page
        assert "/items/1" not in page
        assert "hinoki_build_info" in page
        assert (await fetch(metrics_port, "/metrics")).count(
            "# TYPE hinoki_http_requests_total"
        ) == 1
        assert (await fetch(port, "/metrics")).startswith("HTTP/1.1 404")
    finally:
        await worker.stop()


def scrape(port: int) -> str:
    url = f"http://127.0.0.1:{port}/metrics"
    with urllib.request.urlopen(url, timeout=5) as answer:
        return str(answer.read().decode())


def wait_until(condition: object, seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if callable(condition) and condition():
                return
        except OSError:
            pass
        time.sleep(0.1)
    pytest.fail("timed out")


def test_several_workers_share_one_listener_and_report_every_worker() -> None:
    port, metrics_port = free_port(), free_port()
    environment = {**os.environ, "METRICS_PORT": str(metrics_port)}
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hinoki",
            "serve",
            "tests.kisometricsapp:create_app",
            "--server",
            "kiso",
            "--port",
            str(port),
            "--workers",
            "2",
        ],
        cwd=ROOT,
        env=environment,
    )
    try:

        def ready() -> bool:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/items/1", timeout=2):
                return True

        wait_until(ready)
        for number in range(40):
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/items/{number}", timeout=5
            ):
                pass

        def counted() -> bool:
            # A peer's numbers are as old as its last snapshot, so they arrive
            # within a snapshot interval or two.
            page = scrape(metrics_port)
            total = sum(
                int(line.rsplit(" ", 1)[1])
                for line in page.splitlines()
                if line.startswith(
                    'hinoki_http_requests_total{route="/items/{item_id}"'
                )
            )
            return total == 41 and 'worker="0"' in page and 'worker="1"' in page

        wait_until(counted)
    finally:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=30)


@dataclass(frozen=True)
class Noted:
    text: str


async def test_a_scrape_shows_event_handler_runs_by_outcome() -> None:
    handlers = events.Handlers()

    @handlers.after_commit(Noted)
    async def fine(event: Noted) -> None:
        return None

    @handlers.after_commit(Noted)
    async def broken(event: Noted) -> None:
        raise RuntimeError(event.text)

    @handlers.after_commit(Noted)
    async def slow(event: Noted) -> None:
        await asyncio.sleep(5)

    listener = open_listener("127.0.0.1", 0, 16)
    metrics_port = listener.getsockname()[1]
    options = MetricsSettings(listen=f"127.0.0.1:{metrics_port}").native_options(
        version="test", fd=listener.detach()
    )
    app = Hinoki(
        events=[handlers],
        settings=AppSettings(events=EventsSettings(handler_timeout=0.05)),
    )
    worker = KisoWorker(app, "127.0.0.1", 0, metrics=options)
    await worker.start()
    try:
        with events.use(app.events):
            await events.emit(Noted("private text"))
            await events.emit(Noted("private text"))
        page = await fetch(metrics_port, "/metrics")
        total = 'hinoki_events_total{event="Noted",phase="after_commit",outcome='
        assert f'{total}"ok",worker="0"}} 2' in page
        assert f'{total}"error",worker="0"}} 2' in page
        assert f'{total}"timeout",worker="0"}} 2' in page
        duration = 'hinoki_event_handler_duration_seconds_count{event="Noted",'
        assert f'{duration}phase="after_commit",worker="0"}} 6' in page
        assert "private text" not in page
    finally:
        await worker.stop()
