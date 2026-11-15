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

    assert summary["median"] == 20
    assert (summary["low"], summary["high"]) == (10, 40)
    assert summary["wrong"] == 0
    assert summary["runs"] == 3
    assert summary["cv_pct"] > 15


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
    assert '<table class="benchmark-table">' in rendered
    assert rendered.index("</figure>") < rendered.index("<table")
    assert "Median req/s" in rendered
    assert "Unexpected responses" in rendered
    assert 'scope="col">CV<' in rendered
    assert "Additional capture environment-same-granian-110000.json" in rendered


def test_missing_raw_scenario_fails_instead_of_partial_report() -> None:
    with (
        tempfile.TemporaryDirectory() as temporary,
        pytest.raises(ValueError, match="missing raw results"),
    ):
        report.load_runs(Path(temporary), "hinoki", "list")
