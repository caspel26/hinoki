"""Pure checks for the local generated-list A/B benchmark harness."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from benchmarks.rowwireapp import make_app
from benchmarks.rowwirebench import (
    build_report,
    database_identity,
    ensure_clean_for_timing,
    ensure_empty_output,
    metrics,
    requested_cpus,
)


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


def test_host_cpu_parser_and_database_metadata_avoid_credentials() -> None:
    assert requested_cpus("0-2,5") == {0, 1, 2, 5}
    assert database_identity("postgres://user:secret@db.local:5432/rows") == {
        "scheme": "postgres",
        "host": "db.local",
        "port": "5432",
        "database": "rows",
    }
