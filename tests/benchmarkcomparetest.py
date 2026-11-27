"""Unit tests for benchmarks/ci_compare.py verdict and scheduling logic."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from benchmarks.ci_compare import (
    Thresholds,
    evaluate_runs,
    evaluate_workload,
    pair_schedule,
    render_markdown,
)


def _sample(
    rps: float,
    p95_ms: float = 0.5,
    completed: int = 1000,
    wrong: int = 0,
) -> dict[str, object]:
    return {
        "rps": rps,
        "p50_ms": p95_ms * 0.6,
        "p95_ms": p95_ms,
        "p99_ms": p95_ms * 1.2,
        "completed": completed,
        "wrong": wrong,
        "lane": "native",
        "lane_reason": "native_dispatch",
    }


def test_pair_schedule_alternates_ab_ba() -> None:
    assert pair_schedule(5) == [
        ("base", "cand"),
        ("cand", "base"),
        ("base", "cand"),
        ("cand", "base"),
        ("base", "cand"),
    ]


def test_evaluate_workload_ok_on_minor_jitter() -> None:
    base = [_sample(10000.0, 0.50), _sample(10100.0, 0.51), _sample(9900.0, 0.49)]
    cand = [_sample(9950.0, 0.51), _sample(10050.0, 0.50), _sample(9850.0, 0.50)]
    res = evaluate_workload("health", base, cand, Thresholds())
    assert res["verdict"] == "ok"


def test_evaluate_workload_detects_regression() -> None:
    base = [_sample(10000.0, 0.50) for _ in range(5)]
    cand = [_sample(8000.0, 0.70) for _ in range(5)]
    res = evaluate_workload("health", base, cand, Thresholds())
    assert res["verdict"] == "regression"
    assert res["rps_delta_pct"] < -10.0


def test_evaluate_workload_detects_noisy_samples() -> None:
    base = [
        _sample(10000.0, 0.50),
        _sample(5000.0, 0.50),
        _sample(12000.0, 0.50),
        _sample(6000.0, 0.50),
        _sample(11000.0, 0.50),
    ]
    cand = [
        _sample(9900.0, 0.50),
        _sample(5100.0, 0.50),
        _sample(11900.0, 0.50),
        _sample(6100.0, 0.50),
        _sample(10900.0, 0.50),
    ]
    res = evaluate_workload("health", base, cand, Thresholds())
    assert res["verdict"] == "noisy"


def test_evaluate_workload_invalid_on_http_errors_or_zero_completed() -> None:
    base = [_sample(10000.0, 0.50) for _ in range(5)]
    bad_http = [_sample(10000.0, 0.50, wrong=1) for _ in range(5)]
    zero_done = [_sample(10000.0, 0.50, completed=0) for _ in range(5)]

    assert (
        evaluate_workload("health", base, bad_http, Thresholds())["verdict"]
        == "invalid"
    )
    assert (
        evaluate_workload("health", base, zero_done, Thresholds())["verdict"]
        == "invalid"
    )


def test_evaluate_runs_invalid_when_workload_missing() -> None:
    base_runs = [{"workloads": {"health": _sample(10000.0)}}]
    cand_runs = [{"workloads": {"other": _sample(10000.0)}}]
    status, results = evaluate_runs(base_runs, cand_runs, Thresholds())
    assert status == "invalid"
    assert results["health"]["verdict"] == "invalid"
    assert results["other"]["verdict"] == "invalid"


def test_render_markdown_includes_status_and_workloads() -> None:
    report = {
        "status": "regression",
        "base_sha": "abc1234",
        "cand_sha": "def5678",
        "base_hinoki_file": "/base/hinoki/__init__.py",
        "cand_hinoki_file": "/cand/hinoki/__init__.py",
        "config": {
            "rounds": 5,
            "seconds": 2.0,
            "connections": 16,
            "informational": True,
        },
        "workloads": {
            "health": {
                "name": "health",
                "lane": "native",
                "base_rps_median": 10000.0,
                "cand_rps_median": 8000.0,
                "rps_delta_pct": -20.0,
                "base_p95_median_ms": 0.5,
                "cand_p95_median_ms": 0.7,
                "p95_delta_pct": 40.0,
                "max_cv_pct": 2.1,
                "verdict": "regression",
            }
        },
        "error": "",
    }
    md = render_markdown(report)
    assert "REGRESSION" in md
    assert "(informational)" in md
    assert "`health`" in md


def test_cli_informational_exits_zero_on_regression_and_invalid(tmp_path: Path) -> None:
    fake_bench = tmp_path / "fake_bench.py"
    fake_bench.write_text(
        "import argparse, json\n"
        "from pathlib import Path\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--mode')\n"
        "p.add_argument('--out', type=Path)\n"
        "p.add_argument('--sha', default='')\n"
        "p.add_argument('--seconds', type=float)\n"
        "p.add_argument('--warmup', type=float)\n"
        "p.add_argument('--connections', type=int)\n"
        "p.add_argument('--slowdown-ms', type=float)\n"
        "a = p.parse_args()\n"
        "rps = 5000.0 if a.slowdown_ms > 0 else 10000.0\n"
        "payload = {\n"
        "  'hinoki_file': '/fake/hinoki.py', 'env': {},\n"
        "  'workloads': {'health': {'rps': rps, 'p50_ms': 0.2, 'p95_ms': 0.4, "
        "'p99_ms': 0.5, 'completed': 100, 'wrong': 0, "
        "'lane': 'native', 'lane_reason': 'native_dispatch'}}\n"
        "}\n"
        "a.out.write_text(json.dumps(payload))\n",
        encoding="utf-8",
    )
    out_json = tmp_path / "report.json"
    out_md = tmp_path / "report.md"
    info_proc = subprocess.run(
        [
            sys.executable,
            "benchmarks/ci_compare.py",
            "--base-python",
            sys.executable,
            "--cand-python",
            sys.executable,
            "--bench-script",
            str(fake_bench),
            "--rounds",
            "5",
            "--cand-slowdown-ms",
            "2.0",
            "--out-json",
            str(out_json),
            "--out-md",
            str(out_md),
            "--informational",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert info_proc.returncode == 0
    report = json.loads(out_json.read_text(encoding="utf-8"))
    assert report["status"] == "regression"

    gating_proc = subprocess.run(
        [
            sys.executable,
            "benchmarks/ci_compare.py",
            "--base-python",
            sys.executable,
            "--cand-python",
            sys.executable,
            "--bench-script",
            str(fake_bench),
            "--rounds",
            "5",
            "--cand-slowdown-ms",
            "2.0",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert gating_proc.returncode == 1
