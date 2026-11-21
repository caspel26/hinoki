import contextlib
import io
import json
import runpy
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
import report  # type: ignore[import-not-found]

JSONData = dict[str, Any]


def sample(rate: float) -> JSONData:
    return {
        "summary": {"requestsPerSec": rate},
        "statusCodeDistribution": {"200": 500},
        "errorDistribution": {"aborted due to deadline": 64},
        "latencyPercentiles": {"p50": 0.001, "p95": 0.003, "p99": 0.005},
    }


def write_report_fixture(
    directory: Path, stack: str = "hinoki", workers: int = 1
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "environment.json").write_text(
        json.dumps(
            {
                "date": "2026-11-17",
                "cpu": "CPU",
                "cores": 8,
                "machine": "Linux",
                "loadavg_at_start": [0.1, 0.2, 0.3],
                "postgres": "PostgreSQL 17",
                "oha": "oha 1.17",
                "settings": {
                    "server_mode": "documented",
                    "stacks": [stack],
                    "workers": workers,
                    "server_cpus": None,
                    "load_cpus": None,
                    "concurrency": 64,
                    "warmup_s": 10,
                    "runs": 2,
                    "duration_s": 20,
                },
            }
        )
    )
    (directory / f"{stack}-verify.txt").write_text("OK")
    for scenario in report.SCENARIOS:
        for index, rate in enumerate((10.0, 12.0), start=1):
            (directory / f"{stack}-{scenario}-{index}.json").write_text(
                json.dumps(sample(rate))
            )


def test_summarizes_median_range_and_unexpected_responses() -> None:
    summary = report.run_summary([sample(10), sample(20), sample(40)], "200")
    assert summary["latency_ms"]["p99"] == {"median": 5.0, "low": 5.0, "high": 5.0}

    assert summary["median"] == 20
    assert (summary["low"], summary["high"]) == (10, 40)
    assert summary["wrong"] == 0
    assert summary["runs"] == 3
    assert summary["cv_pct"] > 15


def test_latency_spread_is_median_of_per_run_percentiles_with_run_range() -> None:
    runs = [sample(10), sample(10), sample(10)]
    for run, p95 in zip(runs, (0.002, 0.004, 0.010), strict=True):
        run["latencyPercentiles"]["p95"] = p95

    p95 = report.run_summary(runs, "200")["latency_ms"]["p95"]

    assert p95 == {"median": 4.0, "low": 2.0, "high": 10.0}


def test_report_has_sorted_median_bars_and_a_semantic_table_per_scenario() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        (directory / "environment.json").write_text(
            json.dumps(
                {
                    "date": "2026-11-15",
                    "cpu": "CPU",
                    "cores": 8,
                    "machine": "Linux",
                    "loadavg_at_start": [0.1, 0.2, 0.3],
                    "postgres": "PostgreSQL 17",
                    "oha": "oha 1.17",
                    "settings": {
                        "server_mode": "same-granian",
                        "stacks": ["hinoki-granian"],
                        "workers": 4,
                        "server_cpus": "0-3",
                        "load_cpus": "6-7",
                        "concurrency": 64,
                        "warmup_s": 10,
                        "runs": 2,
                        "duration_s": 20,
                    },
                }
            )
        )
        extra_environment = json.loads((directory / "environment.json").read_text())
        extra_environment["date"] = "2026-11-15 11:00:00 +0100"
        extra_environment["loadavg_at_start"] = [1.0, 1.5, 2.0]
        extra_environment["settings"]["scenarios"] = ["create"]
        (directory / "environment-same-granian-110000.json").write_text(
            json.dumps(extra_environment)
        )
        (directory / "hinoki-granian-verify.txt").write_text("OK")
        for scenario in report.SCENARIOS:
            for index, rate in enumerate((10.0, 12.0), start=1):
                (directory / f"hinoki-granian-{scenario}-{index}.json").write_text(
                    json.dumps(sample(rate))
                )

        rendered = report.render_mode(directory)

    assert rendered.count('class="bench-chart"') == 4
    assert rendered.count('<table class="benchmark-table">') == 4
    assert rendered.count('role="img" aria-labelledby=') == 4
    assert 'scope="rowgroup"' not in rendered
    assert 'scope="row">Hinoki · Granian<' in rendered
    assert 'scope="col">Requests/s<' in rendered
    assert 'scope="col">p99 ms<' in rendered
    assert 'class="bench-bar" style="width:100.00%"' in rendered
    assert "Bars show median requests per second" in rendered
    assert (
        '<span class="bench-name"><b>Hinoki</b><small>Granian</small></span>'
        in rendered
    )
    assert rendered.count('<details class="bench-details">') == 4
    assert rendered.count('<span class="bench-axis" aria-hidden="true">') == 4
    assert rendered.count('<span class="bench-gridlines" aria-hidden="true">') == 4
    assert 'class="bench-row is-hinoki" data-stack="hinoki"' in rendered


def test_value_axis_labels_zero_midpoint_and_maximum() -> None:
    axis = report.value_axis(12000.0)

    assert axis.count("<span>") == 4
    assert "<span>0</span><span>6,000</span><span>12,000</span>" in axis
    assert 'aria-hidden="true"' in axis


def test_stack_key_ignores_the_server_and_normalises_the_name() -> None:
    assert report.stack_key("Django Ninja · Uvicorn") == "django-ninja"
    assert report.stack_key("FastAPI · Uvicorn") == "fastapi"
    assert report.stack_key("Hinoki") == "hinoki"


def test_unstable_scenario_is_withheld_and_stable_ones_are_reported() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        (directory / "environment.json").write_text(
            json.dumps(
                {
                    "date": "2026-11-17",
                    "cpu": "CPU",
                    "cores": 8,
                    "machine": "Linux",
                    "loadavg_at_start": [0.1, 0.2, 0.3],
                    "postgres": "PostgreSQL 17",
                    "oha": "oha 1.17",
                    "settings": {
                        "server_mode": "documented",
                        "stacks": ["hinoki"],
                        "workers": 4,
                        "server_cpus": "0-3",
                        "load_cpus": "6-7",
                        "concurrency": 64,
                        "warmup_s": 10,
                        "runs": 3,
                        "duration_s": 20,
                    },
                }
            )
        )
        (directory / "hinoki-verify.txt").write_text("OK")
        for scenario in report.SCENARIOS:
            rates = (10.0, 40.0, 90.0) if scenario == "create" else (10.0, 10.1, 9.9)
            for index, rate in enumerate(rates, start=1):
                (directory / f"hinoki-{scenario}-{index}.json").write_text(
                    json.dumps(sample(rate))
                )

        rendered = report.render_mode(directory)

    assert 'class="bench-withheld"' in rendered
    assert "Not compared: requests-per-second CV exceeded 15%" in rendered
    assert "GET /health" in rendered
    assert "POST /posts" not in rendered


def test_unpinned_run_is_described_as_unpinned() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment = {
            "date": "2026-11-16",
            "cpu": "Apple M4 Pro",
            "cores": 14,
            "machine": "macOS",
            "loadavg_at_start": [1.0, 1.0, 1.0],
            "postgres": "PostgreSQL 17",
            "oha": "oha 1.17",
            "host": {
                "performance_cores": "10",
                "efficiency_cores": "4",
                "power": "AC Power",
            },
            "settings": {
                "server_mode": "documented",
                "stacks": ["hinoki"],
                "workers": 4,
                "server_cpus": None,
                "load_cpus": None,
                "concurrency": 64,
                "warmup_s": 10,
                "runs": 2,
                "duration_s": 20,
            },
        }
        (directory / "environment.json").write_text(json.dumps(environment))
        (directory / "hinoki-verify.txt").write_text("OK")
        for scenario in report.SCENARIOS:
            for index in (1, 2):
                (directory / f"hinoki-{scenario}-{index}.json").write_text(
                    json.dumps(sample(10.0))
                )

        rendered = report.render_mode(directory)

    assert "no CPU pinning" in rendered
    assert "CPUs None" not in rendered
    assert "Docker Desktop virtual machine" in rendered


def test_missing_raw_scenario_fails_instead_of_partial_report() -> None:
    with (
        tempfile.TemporaryDirectory() as temporary,
        pytest.raises(ValueError, match="missing raw results"),
    ):
        report.load_runs(Path(temporary), "hinoki", "list")


def test_missing_metadata_and_invalid_stack_configuration_are_rejected() -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        with pytest.raises(ValueError, match="missing environment metadata"):
            report.reported_scenarios(directory)
        with pytest.raises(ValueError, match="missing environment metadata"):
            report.render_mode(directory)
        with pytest.raises(ValueError, match="no benchmark stacks"):
            report.stack_names(directory, {})
        with pytest.raises(ValueError, match="unknown stack"):
            report.stack_names(directory, {"stacks": ["unknown"]})
        with pytest.raises(ValueError, match="missing verification output"):
            report.stack_names(directory, {"stacks": ["hinoki"]})


def test_report_cli_generates_a_page_from_retained_sample_files(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        write_report_fixture(directory)

        monkeypatch.setattr(sys, "argv", [str(report.__file__), str(directory)])
        runpy.run_path(str(report.__file__), run_name="__main__")
        output = capsys.readouterr().out

    assert "# Benchmarks" in output
    assert "Hinoki · Granian" in output


def test_report_cli_requires_at_least_one_results_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", [str(report.__file__)])

    with pytest.raises(SystemExit, match=r"usage: report\.py RESULTS_DIR"):
        runpy.run_path(str(report.__file__), run_name="__main__")


def write_workers(directory: Path, stack: str, cpu_times: list[str]) -> None:
    rows = "\n".join(f"{n} 1 {t} worker" for n, t in enumerate(cpu_times, start=2))
    (directory / f"{stack}-workers.txt").write_text(
        f"pid pgid cputime command\n{rows}\n"
    )


def test_stack_with_one_busy_process_is_flagged_but_balanced_stacks_are_not() -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        write_workers(
            directory, "bolt", ["0:00.30", "0:00.02", "0:00.02", "0:00.02", "8:40.38"]
        )
        write_workers(
            directory,
            "hinoki",
            ["0:00.08", "0:00.02", "6:40.31", "10:27.76", "9:55.71", "10:25.25"],
        )
        write_workers(
            directory, "ninja", ["0:00.30", "8:42.06", "8:42.37", "8:42.29", "8:42.83"]
        )

        flagged = report.single_process_stacks(
            directory, ["bolt", "hinoki", "ninja", "fastapi"], 4
        )

    assert flagged == ["bolt"]


def test_report_explains_when_a_stack_is_excluded_for_worker_imbalance() -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        write_report_fixture(directory, stack="bolt", workers=4)
        write_workers(directory, "bolt", ["10:00.00", "0:05.00", "0:05.00", "0:05.00"])

        rendered = report.render_mode(directory)

    assert "Django Bolt · Actix is not reported here" in rendered
    assert "one process did nearly all the work" in rendered


def test_cpu_time_accepts_macos_and_linux_ps_formats() -> None:
    assert report.cpu_seconds("1:02.50") == 62.5
    assert report.cpu_seconds("00:05:59") == 359
    assert report.cpu_seconds("01:02:03") == 3723
    assert report.cpu_seconds("2-00:00:01") == 172801


def test_each_chart_uses_its_own_zero_based_scale_and_sorts_medians() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        (directory / "environment.json").write_text(
            json.dumps(
                {
                    "date": "2026-11-17",
                    "cpu": "CPU",
                    "cores": 8,
                    "machine": "Linux",
                    "loadavg_at_start": [0.1, 0.2, 0.3],
                    "postgres": "PostgreSQL 17",
                    "oha": "oha 1.17",
                    "settings": {
                        "server_mode": "same-granian",
                        "stacks": ["hinoki-granian", "fastapi-granian"],
                        "workers": 4,
                        "server_cpus": "0-3",
                        "load_cpus": "6-7",
                        "concurrency": 64,
                        "warmup_s": 10,
                        "runs": 3,
                        "duration_s": 20,
                    },
                }
            )
        )
        for stack in ("hinoki-granian", "fastapi-granian"):
            (directory / f"{stack}-verify.txt").write_text("OK")
            for scenario in report.SCENARIOS:
                rates = (
                    (30.0, 32.0, 28.0)
                    if stack == "hinoki-granian"
                    else (18.0, 20.0, 19.0)
                )
                for index, rate in enumerate(rates, start=1):
                    (directory / f"{stack}-{scenario}-{index}.json").write_text(
                        json.dumps(sample(rate))
                    )

        rendered = report.render_mode(directory)

    assert rendered.index("Hinoki · Granian") < rendered.index("FastAPI · Granian")
    assert "width:100.00%" in rendered
    assert "width:63.33%" in rendered
    assert "Median shown as a point" not in rendered


def write_pair_fixture(
    directory: Path, rates: dict[str, tuple[float, ...]], create_rates: Any = None
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "environment.json").write_text(
        json.dumps(
            {
                "date": "2026-11-17",
                "cpu": "CPU",
                "cores": 8,
                "machine": "Linux",
                "loadavg_at_start": [0.1, 0.2, 0.3],
                "postgres": "PostgreSQL 17",
                "oha": "oha 1.17",
                "settings": {
                    "server_mode": "same-granian",
                    "stacks": list(rates),
                    "workers": 4,
                    "server_cpus": "0-3",
                    "load_cpus": "6-7",
                    "concurrency": 64,
                    "warmup_s": 10,
                    "runs": 3,
                    "duration_s": 20,
                },
            }
        )
    )
    for stack, stack_rates in rates.items():
        (directory / f"{stack}-verify.txt").write_text("OK")
        for scenario in report.SCENARIOS:
            values = (
                create_rates
                if scenario == "create" and create_rates and stack == "hinoki-granian"
                else stack_rates
            )
            for index, rate in enumerate(values, start=1):
                (directory / f"{stack}-{scenario}-{index}.json").write_text(
                    json.dumps(sample(rate))
                )


def test_each_scenario_states_a_headline_and_a_multiplier_over_the_slowest() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        write_pair_fixture(
            directory,
            {
                "hinoki-granian": (30.0, 32.0, 28.0),
                "fastapi-granian": (18.0, 20.0, 19.0),
            },
        )
        rendered = report.render_mode(directory)

    assert (
        "Highest median: <b>Hinoki · Granian</b> at 30 requests/s, "
        "1.58&times; the slowest stack (FastAPI · Granian)."
    ) in rendered
    assert '<span class="bench-ratio">1.58&times;</span>' in rendered
    assert '<span class="bench-ratio">1.00&times;</span>' in rendered
    assert rendered.count('<div class="bench-row is-hinoki" data-stack="hinoki">') == 4
    assert rendered.count('<div class="bench-row" data-stack="fastapi">') == 4
    assert "overlap" not in rendered


def test_headline_discloses_when_the_top_two_run_ranges_overlap() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        write_pair_fixture(
            directory,
            {
                "hinoki-granian": (30.0, 31.0, 26.0),
                "fastapi-granian": (28.0, 29.0, 27.0),
            },
        )
        rendered = report.render_mode(directory)

    assert (
        "The run ranges of the top two stacks overlap, so their order is not settled."
        in rendered
    )


def test_summary_cards_link_every_scenario_including_withheld_ones() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary) / "2026-11-17" / "same-granian"
        write_pair_fixture(
            directory,
            {
                "hinoki-granian": (30.0, 30.5, 29.5),
                "fastapi-granian": (18.0, 18.5, 17.5),
            },
            create_rates=(10.0, 40.0, 90.0),
        )
        rendered = report.render_mode(directory)

    assert rendered.count('class="bench-card"') == 3
    assert rendered.count('class="bench-card is-withheld"') == 1
    assert 'href="#bench-2026-11-17-same-granian-detail"' in rendered
    assert (
        '<section class="bench-scenario" id="bench-2026-11-17-same-granian-detail"'
        in rendered
    )
    assert 'href="#bench-2026-11-17-same-granian-withheld"' in rendered


def test_single_stack_view_shows_values_without_a_multiplier() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        write_pair_fixture(directory, {"hinoki-kiso": (30.0, 31.0, 29.0)})
        rendered = report.render_mode(directory)

    assert "bench-ratio" not in rendered
    assert "Median: <b>Hinoki · Kiso</b> at 30 requests/s." in rendered


def test_page_has_no_front_matter_and_only_describes_rendered_setups(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        results = Path(temporary) / "results" / "2026-11-17"
        for mode in ("documented", "same-granian"):
            write_report_fixture(results / mode, stack="hinoki", workers=4)
            env_path = results / mode / "environment.json"
            env = json.loads(env_path.read_text())
            env["settings"]["server_mode"] = mode
            env_path.write_text(json.dumps(env))
        report.main([results / "documented", results / "same-granian"])
        page = capsys.readouterr().out

    assert not page.startswith("---")
    assert "status:" not in page
    assert "## One API. Two server setups." in page
    assert "Kiso" not in page
    assert "--server-mode kiso" not in page
    assert (
        "compare each stack on its documented server or compare the three ASGI stacks on Granian"
        in page
    )


def test_page_has_the_sections_a_reader_needs_in_order() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary) / "results" / "documented"
        directory.mkdir(parents=True)
        (directory / "environment.json").write_text(
            json.dumps(
                {
                    "date": "2026-11-17",
                    "cpu": "CPU",
                    "cores": 8,
                    "machine": "Linux",
                    "loadavg_at_start": [0.1, 0.2, 0.3],
                    "postgres": "PostgreSQL 17",
                    "oha": "oha 1.17",
                    "packages": json.dumps([{"name": "hinoki", "version": "0.0.1"}]),
                    "settings": {
                        "server_mode": "documented",
                        "stacks": ["hinoki"],
                        "workers": 4,
                        "server_cpus": "0-3",
                        "load_cpus": "6-7",
                        "concurrency": 64,
                        "warmup_s": 10,
                        "runs": 3,
                        "duration_s": 20,
                    },
                }
            )
        )
        (directory / "hinoki-verify.txt").write_text("OK")
        for scenario in report.SCENARIOS:
            for index in (1, 2, 3):
                (directory / f"hinoki-{scenario}-{index}.json").write_text(
                    json.dumps(sample(10.0))
                )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            report.main([directory])
        page = buffer.getvalue()

    headings = [
        "# Benchmarks",
        "## Results",
        "## What was measured",
        "## How to read the numbers",
        "## Reproduce",
    ]
    positions = [page.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert "hinoki 0.0.1" in page
    assert '=== "Documented servers"' in page
    assert '!!! warning "Read this first"' in page
    assert page.index("FIELD NOTE") < page.index("# Benchmarks")


def test_kiso_view_is_its_own_tab_with_a_single_stack() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary) / "results" / "kiso"
        write_report_fixture(directory, stack="hinoki-kiso", workers=4)
        env_path = directory / "environment.json"
        env = json.loads(env_path.read_text())
        env["settings"]["server_mode"] = "kiso"
        env_path.write_text(json.dumps(env))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            report.main([directory])
        page = buffer.getvalue()

    assert '=== "Hinoki on Kiso"' in page
    assert "Hinoki · Kiso" in page


def test_run_py_offers_a_kiso_mode_that_serves_with_the_rust_server() -> None:
    import run  # type: ignore[import-not-found]

    assert run.SERVER_MODES["kiso"] is run.KISO_SERVERS
    command, _ = run.KISO_SERVERS["hinoki-kiso"]
    assert command[:2] == ["hinoki", "serve"]
    assert command[command.index("--server") + 1] == "kiso"
    assert run.LABELS["hinoki-kiso"] == report.FRAMEWORKS["hinoki-kiso"]
    assert not set(run.KISO_SERVERS) & (
        set(run.DOCUMENTED_SERVERS) | set(run.GRANIAN_SERVERS)
    )


def test_a_sample_waits_for_the_load_to_fall_and_records_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import run  # type: ignore[import-not-found]

    loads = iter([2.5, 1.8, 0.9])
    sleeps: list[float] = []
    monkeypatch.setattr(run.os, "getloadavg", lambda: (next(loads), 0.0, 0.0))
    monkeypatch.setattr(run.time, "sleep", sleeps.append)
    log = tmp_path / "loadavg.txt"

    run.wait_for_quiet(1.0, log, "hinoki-kiso-list-1")

    assert sleeps == [run.QUIET_POLL, run.QUIET_POLL]
    assert log.read_text() == "hinoki-kiso-list-1 load1=0.90\n"


def test_a_host_that_stays_busy_stops_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import run  # type: ignore[import-not-found]

    clock = iter(range(0, 10_000, 600))
    monkeypatch.setattr(run.os, "getloadavg", lambda: (3.0, 0.0, 0.0))
    monkeypatch.setattr(run.time, "sleep", lambda _: None)
    monkeypatch.setattr(run.time, "monotonic", lambda: next(clock))

    with pytest.raises(SystemExit, match="load average stayed above"):
        run.wait_for_quiet(1.0, tmp_path / "loadavg.txt", "x")
    assert not (tmp_path / "loadavg.txt").exists()


def test_without_a_limit_the_load_is_only_recorded(tmp_path: Path) -> None:
    import run  # type: ignore[import-not-found]

    run.wait_for_quiet(None, tmp_path / "loadavg.txt", "x")

    assert (tmp_path / "loadavg.txt").read_text().startswith("x load1=")
