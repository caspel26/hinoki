"""Pure checks for the local generated-list A/B benchmark harness."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from benchmarks import rowwirebench
from benchmarks.rowwireapp import make_app
from benchmarks.rowwirebench import (
    build_report,
    collect_sample,
    database_identity,
    ensure_clean_for_timing,
    ensure_empty_output,
    ensure_port_available,
    metrics,
    requested_cpus,
)


class SampleHarnessDoubles:
    def __init__(self, loads: list[list[float] | None]) -> None:
        self.events: list[str] = []
        self.loads = iter(loads)
        self.pid = 12345
        self.quiet_error: RuntimeError | None = None

    async def seed_database(self, _row_count: int) -> None:
        self.events.append("seed")

    def start_server(
        self, _variant: str, _args: argparse.Namespace, _path: Path
    ) -> Any:
        self.events.append("start")
        return self

    def wait_ready(self, _port: int) -> None:
        self.events.append("ready")

    def warmup(self, _url: str, _args: argparse.Namespace) -> None:
        self.events.append("warmup")

    def wait_for_quiet_host(self) -> dict[str, Any]:
        self.events.append("quiet")
        if self.quiet_error is not None:
            raise self.quiet_error
        return {
            "load_average": [0.9, 0.9, 0.9],
            "wait_seconds": 10.0,
            "polls": 6,
            "max_load_while_waiting": 1.4,
            "active_processes": [],
        }

    def load_average(self) -> list[float] | None:
        self.events.append("load")
        return next(self.loads)

    def run(self, *_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.events.append("oha")
        return subprocess.CompletedProcess(
            args=["oha"],
            returncode=0,
            stdout=json.dumps(
                {
                    "summary": {"requestsPerSec": 1000},
                    "latencyPercentiles": {"p50": 0.01, "p99": 0.02},
                }
            ),
            stderr="",
        )

    def stop_server(self, _process: object) -> None:
        self.events.append("stop")

    def sleep(self, _seconds: float) -> None:
        self.events.append("sleep")

    def oha_command(self, _url: str, _args: argparse.Namespace) -> list[str]:
        return ["oha"]

    def pin(self, _cpus: str) -> list[str]:
        return ["taskset"]

    def command_output(self, _command: list[str]) -> str:
        return "pid 12345's current affinity list: 0-3"


class QuietWindowClock:
    def __init__(self, loads: list[list[float] | None]) -> None:
        self.loads = iter(loads)
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def load_average(self) -> list[float] | None:
        return next(self.loads)

    def command_output(self, _command: list[str]) -> str:
        return ""


def install_sample_doubles(
    monkeypatch: pytest.MonkeyPatch, doubles: SampleHarnessDoubles
) -> None:
    monkeypatch.setattr(rowwirebench, "seed_database", doubles.seed_database)
    monkeypatch.setattr(rowwirebench, "start_server", doubles.start_server)
    monkeypatch.setattr(rowwirebench, "wait_ready", doubles.wait_ready)
    monkeypatch.setattr(rowwirebench, "warmup", doubles.warmup)
    monkeypatch.setattr(
        rowwirebench, "wait_for_quiet_host", doubles.wait_for_quiet_host
    )
    monkeypatch.setattr(rowwirebench, "load_average", doubles.load_average)
    monkeypatch.setattr(subprocess, "run", doubles.run)
    monkeypatch.setattr(rowwirebench, "stop_server", doubles.stop_server)
    monkeypatch.setattr(time, "sleep", doubles.sleep)
    monkeypatch.setattr(rowwirebench, "oha_command", doubles.oha_command)
    monkeypatch.setattr(rowwirebench, "pin", doubles.pin)
    monkeypatch.setattr(rowwirebench, "command_output", doubles.command_output)


def install_quiet_window_clock(
    monkeypatch: pytest.MonkeyPatch, clock: QuietWindowClock
) -> None:
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    monkeypatch.setattr(rowwirebench, "load_average", clock.load_average)
    monkeypatch.setattr(rowwirebench, "command_output", clock.command_output)


def test_variants_share_the_same_generated_route_and_change_only_row_wire() -> None:
    wire = make_app("wire")
    python = make_app("python")
    wire_binding = next(
        binding for binding in wire.bindings if binding.route.path == "/records"
    )
    python_binding = next(
        binding for binding in python.bindings if binding.route.path == "/records"
    )

    assert wire_binding.row_wire is not None
    assert python_binding.row_wire is None
    assert wire_binding.route.handler is python_binding.route.handler
    assert wire_binding.route.method == python_binding.route.method
    assert wire_binding.params == python_binding.params
    assert wire_binding.route.renderers == python_binding.route.renderers


def test_oha_metrics_convert_response_latency_to_milliseconds() -> None:
    measured = metrics(
        {
            "summary": {"requestsPerSec": 1200.0},
            "latencyPercentiles": {"p50": 0.012, "p99": 0.045},
        }
    )
    assert measured == {
        "requests_per_second": 1200.0,
        "p50_ms": 12.0,
        "p99_ms": 45.0,
    }


def test_report_pairs_runs_and_applies_existing_investigation_policy() -> None:
    samples: list[dict[str, Any]] = []
    for run in range(1, 6):
        samples.extend(
            [
                {
                    "run": run,
                    "page_size": 20,
                    "variant": "wire",
                    "requests_per_second": 1200.0,
                    "p50_ms": 10.0,
                    "p99_ms": 30.0,
                },
                {
                    "run": run,
                    "page_size": 20,
                    "variant": "python",
                    "requests_per_second": 1000.0,
                    "p50_ms": 20.0,
                    "p99_ms": 50.0,
                },
            ]
        )

    report = build_report(samples)
    comparison = report["comparison"]["20"]
    assert comparison["requests_per_second"]["paired_change_pct"] == [20.0] * 5
    assert comparison["requests_per_second"]["detector_signal"] is True
    assert comparison["p50_ms"]["median_change_pct"] == -50.0
    assert comparison["p50_ms"]["detector_signal"] is True
    assert report["variants"]["20"]["wire"]["p99_ms"]["median"] == 30.0


def test_partial_campaign_reports_missing_samples_without_fabricating_statistics() -> (
    None
):
    report = build_report(
        [
            {
                "run": 1,
                "page_size": 20,
                "variant": "wire",
                "requests_per_second": 1000.0,
                "p50_ms": 1.0,
                "p99_ms": 2.0,
            }
        ],
        page_sizes=(20, 100),
    )

    wire = report["variants"]["20"]["wire"]
    python = report["variants"]["20"]["python"]
    assert wire["requests_per_second"] == {
        "sample_count": 1,
        "median": 1000.0,
        "min": 1000.0,
        "max": 1000.0,
        "cv_pct": None,
    }
    assert python["requests_per_second"] is None
    assert report["variants"]["100"]["wire"]["requests_per_second"] is None
    comparison = report["comparison"]["20"]["requests_per_second"]
    assert comparison["paired_sample_count"] == 0
    assert comparison["median_change_pct"] is None
    assert comparison["paired_change_stdev_pct"] is None
    assert comparison["interpretation"] == "insufficient paired samples"

    paired_report = build_report(
        [
            {
                "run": 1,
                "page_size": 20,
                "variant": "wire",
                "requests_per_second": 1000.0,
                "p50_ms": 1.0,
                "p99_ms": 2.0,
            },
            {
                "run": 1,
                "page_size": 20,
                "variant": "python",
                "requests_per_second": 500.0,
                "p50_ms": 2.0,
                "p99_ms": 4.0,
            },
        ]
    )
    paired_comparison = paired_report["comparison"]["20"]["requests_per_second"]
    assert paired_comparison["paired_sample_count"] == 1
    assert paired_comparison["median_change_pct"] == 100.0
    assert paired_comparison["paired_change_stdev_pct"] is None
    assert paired_comparison["interpretation"] == "insufficient paired samples"


def test_output_directory_refuses_to_mix_campaign_artifacts(tmp_path: Path) -> None:
    output = tmp_path / "campaign"
    output.mkdir()
    (output / "old-raw.json").write_text("{}")

    with pytest.raises(SystemExit, match="choose a new path"):
        ensure_empty_output(output)

    fresh = tmp_path / "fresh-campaign"
    ensure_empty_output(fresh)
    assert fresh.is_dir()


def test_timed_run_requires_clean_committed_source_but_preflight_does_not() -> None:
    with pytest.raises(SystemExit, match="clean committed worktree"):
        ensure_clean_for_timing(" M python/hinoki/crud.py", verify_only=False)

    ensure_clean_for_timing(" M python/hinoki/crud.py", verify_only=True)
    ensure_clean_for_timing("", verify_only=False)


def test_sample_waits_for_quiet_after_warmup_and_records_final_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    doubles = SampleHarnessDoubles([[0.8, 0.9, 0.9], [1.4, 1.2, 1.0]])
    install_sample_doubles(monkeypatch, doubles)
    args = argparse.Namespace(
        seed_rows=100,
        port=8891,
        warmup=10,
        duration=20,
        connections=64,
        workers=1,
        pool_size=10,
        server_cpus="0-3",
        load_cpus="6-7",
    )

    sample = collect_sample("wire", 20, 1, ("wire", "python"), args, tmp_path)

    assert doubles.events == [
        "seed",
        "start",
        "ready",
        "warmup",
        "quiet",
        "load",
        "oha",
        "load",
        "stop",
        "sleep",
    ]
    assert sample["load_before"] == [0.8, 0.9, 0.9]
    assert sample["requests_per_second"] == 1000.0


@pytest.mark.parametrize("load", [[1.01, 1.0, 1.0], None])
def test_sample_refuses_measurement_when_final_load_is_over_limit_or_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    load: list[float] | None,
) -> None:
    doubles = SampleHarnessDoubles([load])
    install_sample_doubles(monkeypatch, doubles)
    args = argparse.Namespace(
        seed_rows=100,
        port=8891,
        warmup=10,
        duration=20,
        connections=64,
        workers=1,
        pool_size=10,
        server_cpus="0-3",
        load_cpus="6-7",
    )

    with pytest.raises(RuntimeError, match="immediately before timing"):
        collect_sample("wire", 20, 1, ("wire", "python"), args, tmp_path)

    assert "oha" not in doubles.events
    assert doubles.events[-2:] == ["stop", "sleep"]


def test_sample_timeout_after_warmup_stops_server_without_measuring(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    doubles = SampleHarnessDoubles([])
    doubles.quiet_error = RuntimeError("quiet window timed out")
    install_sample_doubles(monkeypatch, doubles)
    args = argparse.Namespace(
        seed_rows=100,
        port=8891,
        warmup=10,
        duration=20,
        connections=64,
        workers=1,
        pool_size=10,
        server_cpus="0-3",
        load_cpus="6-7",
    )

    with pytest.raises(RuntimeError, match="quiet window timed out"):
        collect_sample("wire", 20, 1, ("wire", "python"), args, tmp_path)

    assert "oha" not in doubles.events
    assert doubles.events[-2:] == ["stop", "sleep"]


def test_quiet_window_starts_after_load_drops_and_stays_below_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = QuietWindowClock(
        [
            [1.5, 1.2, 1.2],
            [1.2, 1.2, 1.2],
            [0.9, 1.0, 1.1],
            [0.8, 1.0, 1.1],
            [0.8, 1.0, 1.1],
            [0.7, 1.0, 1.1],
            [0.7, 1.0, 1.1],
            [0.7, 1.0, 1.1],
        ]
    )
    install_quiet_window_clock(monkeypatch, clock)

    result = rowwirebench.wait_for_quiet_host()

    assert result["wait_seconds"] == 14.0
    assert result["polls"] == 8
    assert result["load_average"] == [0.7, 1.0, 1.1]
    assert result["max_load_while_waiting"] == 1.5


def test_quiet_window_times_out_if_load_average_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = QuietWindowClock([None, None, None, None])
    install_quiet_window_clock(monkeypatch, clock)
    monkeypatch.setattr(rowwirebench, "QUIET_TIMEOUT_SECONDS", 4.0)

    with pytest.raises(RuntimeError, match="host did not stay quiet"):
        rowwirebench.wait_for_quiet_host()

    assert clock.now == 4.0


def test_port_check_rejects_an_active_listener() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        with pytest.raises(SystemExit, match=r"benchmark port .* is unavailable"):
            ensure_port_available(port)


def test_port_check_reuses_a_recently_closed_local_port() -> None:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    client = socket.socket()
    client.connect(("127.0.0.1", port))
    accepted, _address = listener.accept()
    accepted.close()
    listener.close()
    client.recv(1)
    client.close()

    ensure_port_available(port)


def test_host_cpu_parser_and_database_metadata_avoid_credentials() -> None:
    assert requested_cpus("0-2,5") == {0, 1, 2, 5}
    assert database_identity("postgres://user:secret@db.local:5432/rows") == {
        "scheme": "postgres",
        "host": "db.local",
        "port": "5432",
        "database": "rows",
    }
