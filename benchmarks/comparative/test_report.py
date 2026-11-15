import json
import tempfile
from pathlib import Path

import pytest
import report


def sample(rate: float) -> dict:
    return {
        "summary": {"requestsPerSec": rate},
        "statusCodeDistribution": {"200": 500},
        "errorDistribution": {"aborted due to deadline": 64},
        "latencyPercentiles": {"p50": 0.001, "p95": 0.003, "p99": 0.005},
    }


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


def test_axis_ceiling_is_a_round_number_above_the_data() -> None:
    assert report.nice_ceiling(867.1) == 1000
    assert report.nice_ceiling(70.0) == 100
    assert report.nice_ceiling(4.4) == 5


def test_chart_has_accessible_label_and_table_is_next_to_it() -> None:
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

    assert 'role="img"' in rendered
    assert "aria-labelledby=" in rendered
    assert "lower is better" in rendered
    assert '<table class="benchmark-table">' in rendered
    assert rendered.index("</figure>") < rendered.index("<table")
    assert 'scope="col">p99 ms<' in rendered
    assert "Unexpected responses" in rendered
    assert "no CPU pinning" not in rendered
    assert rendered.index("response time") < rendered.index("Throughput (secondary)")
    assert 'scope="col">Median req/s<' in rendered.split("Throughput (secondary)")[1]
    assert 'scope="col">CV<' in rendered
    assert "Additional capture environment-same-granian-110000.json" in rendered


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
