"""Paired base-vs-candidate benchmark comparison for Hinoki CI.

Executes warm-up and alternating ``AB, BA, AB, BA, AB`` rounds of
``benchmarks/ci_bench.py`` in separate subprocesses for base and candidate
environments, evaluates ``invalid`` / ``regression`` / ``noisy`` / ``ok``
verdicts, and emits JSON and Markdown summaries.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Thresholds:
    rps_regression_pct: float = 10.0
    p95_regression_pct: float = 15.0
    noise_cv_pct: float = 15.0


@dataclass(frozen=True, slots=True)
class RunConfig:
    rounds: int = 5
    seconds: float = 2.0
    warmup: float = 0.5
    connections: int = 16
    cand_slowdown_ms: float = 0.0
    informational: bool = False


@dataclass(frozen=True, slots=True)
class CompareTarget:
    base_python: Path
    cand_python: Path
    bench_script: Path
    base_sha: str
    cand_sha: str


def pair_schedule(rounds: int) -> list[tuple[str, str]]:
    """Return alternating ('base', 'cand') / ('cand', 'base') round order."""
    return [
        ("base", "cand") if idx % 2 == 0 else ("cand", "base") for idx in range(rounds)
    ]


MIN_CV_SAMPLES = 2


def _cv_percent(values: list[float]) -> float:
    if len(values) < MIN_CV_SAMPLES:
        return 0.0
    mean = statistics.mean(values)
    if mean <= 0.0:
        return 0.0
    return 100.0 * statistics.pstdev(values) / mean


def _sample_valid(sample: dict[str, Any] | None) -> bool:
    if sample is None:
        return False
    completed = int(sample.get("completed", 0))
    wrong = int(sample.get("wrong", 0))
    rps = float(sample.get("rps", 0.0))
    p95 = float(sample.get("p95_ms", 0.0))
    return (
        completed > 0
        and wrong == 0
        and math.isfinite(rps)
        and rps > 0.0
        and math.isfinite(p95)
        and p95 >= 0.0
    )


def _compute_stats(
    base_samples: list[dict[str, Any]],
    cand_samples: list[dict[str, Any]],
) -> dict[str, float]:
    base_rps = [float(s["rps"]) for s in base_samples]
    cand_rps = [float(s["rps"]) for s in cand_samples]
    base_p95 = [float(s["p95_ms"]) for s in base_samples]
    cand_p95 = [float(s["p95_ms"]) for s in cand_samples]

    base_rps_med = statistics.median(base_rps)
    cand_rps_med = statistics.median(cand_rps)
    base_p95_med = statistics.median(base_p95)
    cand_p95_med = statistics.median(cand_p95)

    rps_ratios = [c / b for b, c in zip(base_rps, cand_rps, strict=True)]
    p95_ratios = [
        (c / b if b > 0.0 else 1.0) for b, c in zip(base_p95, cand_p95, strict=True)
    ]
    return {
        "base_rps_median": base_rps_med,
        "cand_rps_median": cand_rps_med,
        "rps_delta_pct": ((cand_rps_med - base_rps_med) / base_rps_med) * 100.0,
        "paired_rps_ratio_median": statistics.median(rps_ratios),
        "base_p95_median_ms": base_p95_med,
        "cand_p95_median_ms": cand_p95_med,
        "p95_delta_pct": (
            ((cand_p95_med - base_p95_med) / base_p95_med) * 100.0
            if base_p95_med > 0.0
            else 0.0
        ),
        "paired_p95_ratio_median": statistics.median(p95_ratios),
        "max_cv_pct": max(
            _cv_percent(base_rps),
            _cv_percent(cand_rps),
            _cv_percent(rps_ratios),
        ),
    }


def evaluate_workload(
    name: str,
    base_samples: list[dict[str, Any]],
    cand_samples: list[dict[str, Any]],
    thresholds: Thresholds,
) -> dict[str, Any]:
    """Evaluate one workload across paired base and candidate samples."""
    if (
        not base_samples
        or len(base_samples) != len(cand_samples)
        or not all(_sample_valid(s) for s in base_samples)
        or not all(_sample_valid(s) for s in cand_samples)
    ):
        return {
            "name": name,
            "verdict": "invalid",
            "reason": "missing, incomplete, or error-status samples",
            "samples": len(base_samples),
        }

    stats = _compute_stats(base_samples, cand_samples)
    rps_drop = stats["rps_delta_pct"] <= -thresholds.rps_regression_pct and (
        stats["paired_rps_ratio_median"] <= 1.0 - thresholds.rps_regression_pct / 100.0
    )
    p95_rise = stats["p95_delta_pct"] >= thresholds.p95_regression_pct and (
        stats["paired_p95_ratio_median"] >= 1.0 + thresholds.p95_regression_pct / 100.0
    )
    if rps_drop or p95_rise:
        verdict = "regression"
        reason = "throughput drop or p95 latency rise exceeded threshold"
    elif stats["max_cv_pct"] > thresholds.noise_cv_pct:
        verdict = "noisy"
        reason = f"sample CV% ({stats['max_cv_pct']:.1f}%) exceeded noise limit"
    else:
        verdict = "ok"
        reason = "within thresholds"

    return {
        "name": name,
        "verdict": verdict,
        "reason": reason,
        "samples": len(base_samples),
        "lane": cand_samples[0].get("lane", "unknown"),
        "lane_reason": cand_samples[0].get("lane_reason", "unknown"),
        **{k: round(v, 3) for k, v in stats.items()},
    }


def overall_verdict(verdicts: list[str]) -> str:
    """Combine per-workload verdicts by severity: invalid > regression > noisy > ok."""
    for target in ("invalid", "regression", "noisy"):
        if target in verdicts:
            return target
    return "ok" if verdicts else "invalid"


def evaluate_runs(
    base_runs: list[dict[str, Any]],
    cand_runs: list[dict[str, Any]],
    thresholds: Thresholds,
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Evaluate all workloads from collected base and candidate runs."""
    if not base_runs or len(base_runs) != len(cand_runs):
        return "invalid", {}
    names: list[str] = []
    for run in (*base_runs, *cand_runs):
        for name in run.get("workloads", {}):
            if name not in names:
                names.append(name)

    results: dict[str, dict[str, Any]] = {}
    for name in names:
        b_samples = [r.get("workloads", {}).get(name) for r in base_runs]
        c_samples = [r.get("workloads", {}).get(name) for r in cand_runs]
        if any(s is None for s in b_samples) or any(s is None for s in c_samples):
            results[name] = {
                "name": name,
                "verdict": "invalid",
                "reason": "missing workload in one or more rounds",
                "samples": len(base_runs),
            }
        else:
            results[name] = evaluate_workload(
                name,
                [s for s in b_samples if s is not None],
                [s for s in c_samples if s is not None],
                thresholds,
            )
    status = overall_verdict([r["verdict"] for r in results.values()])
    return status, results


def _invoke_bench(
    python_bin: Path,
    bench_script: Path,
    sha: str,
    cfg: RunConfig,
    slowdown_ms: float,
    out_path: Path,
) -> dict[str, Any]:
    cmd = [
        str(python_bin),
        str(bench_script),
        "--mode",
        "bench",
        "--out",
        str(out_path),
        "--sha",
        sha,
        "--seconds",
        str(cfg.seconds),
        "--warmup",
        str(cfg.warmup),
        "--connections",
        str(cfg.connections),
        "--slowdown-ms",
        str(slowdown_ms),
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)  # noqa: S603
    if proc.returncode != 0:
        raise RuntimeError(
            f"benchmark command failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    data: dict[str, Any] = json.loads(out_path.read_text(encoding="utf-8"))
    return data


def _format_row(item: dict[str, Any]) -> str:
    name = item["name"]
    verdict = item["verdict"].upper()
    if item["verdict"] == "invalid":
        return f"| `{name}` | - | - | - | - | - | - | - | - | **{verdict}** |"
    return (
        f"| `{name}` | {item.get('lane', '-')} | "
        f"{item['base_rps_median']:.0f} | {item['cand_rps_median']:.0f} | "
        f"{item['rps_delta_pct']:+.1f}% | "
        f"{item['base_p95_median_ms']:.3f} ms | {item['cand_p95_median_ms']:.3f} ms | "
        f"{item['p95_delta_pct']:+.1f}% | "
        f"{item['max_cv_pct']:.1f}% | **{verdict}** |"
    )


def render_markdown(report: dict[str, Any]) -> str:
    """Render a human-readable Markdown summary for GitHub step summaries."""
    status = str(report["status"]).upper()
    mode = "informational" if report["config"]["informational"] else "gating"
    lines = [
        f"## Hinoki Benchmark Comparison: **{status}** ({mode})",
        "",
        f"- **Base SHA:** `{report['base_sha']}` (`{report['base_hinoki_file']}`)",
        f"- **Candidate SHA:** `{report['cand_sha']}` (`{report['cand_hinoki_file']}`)",
        (
            f"- **Rounds:** {report['config']['rounds']} alternating pairs "
            f"({report['config']['seconds']}s each, "
            f"{report['config']['connections']} conns)"
        ),
        "",
        "| Workload | Lane | Base RPS | Cand RPS | RPS Δ | "
        "Base p95 | Cand p95 | p95 Δ | Max CV% | Verdict |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    lines.extend(_format_row(item) for item in report["workloads"].values())
    if report.get("error"):
        lines.extend(["", f"> **Harness Error:** `{report['error']}`"])
    return "\n".join(lines) + "\n"


def _write_outputs(
    report: dict[str, Any], out_json: Path | None, out_md: Path | None
) -> None:
    md_text = render_markdown(report)
    if out_json is not None:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(md_text, encoding="utf-8")
    print(md_text)


def _run_side(
    side: str,
    round_idx: int,
    tmp_dir: Path,
    target: CompareTarget,
    cfg: RunConfig,
) -> tuple[bool, dict[str, Any]]:
    is_base = side == "base"
    out_file = tmp_dir / f"round_{round_idx}_{side}.json"
    run_data = _invoke_bench(
        target.base_python if is_base else target.cand_python,
        target.bench_script,
        target.base_sha if is_base else target.cand_sha,
        cfg,
        0.0 if is_base else cfg.cand_slowdown_ms,
        out_file,
    )
    return is_base, run_data


def _collect_rounds(
    target: CompareTarget,
    cfg: RunConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    base_runs: list[dict[str, Any]] = []
    cand_runs: list[dict[str, Any]] = []
    schedule = pair_schedule(cfg.rounds)
    with tempfile.TemporaryDirectory(prefix="hinoki-ci-compare-") as tmp:
        tmp_dir = Path(tmp)
        try:
            for idx, order in enumerate(schedule):
                for side in order:
                    is_base, run_data = _run_side(side, idx, tmp_dir, target, cfg)
                    (base_runs if is_base else cand_runs).append(run_data)
        except Exception as exc:
            return base_runs, cand_runs, str(exc)
    return base_runs, cand_runs, ""


def run_comparison(
    target: CompareTarget,
    cfg: RunConfig,
    thresholds: Thresholds,
) -> dict[str, Any]:
    """Execute paired rounds and return the structured comparison report."""
    schedule = pair_schedule(cfg.rounds)
    base_runs, cand_runs, error_msg = _collect_rounds(target, cfg)
    status, workloads = (
        ("invalid", {})
        if error_msg
        else evaluate_runs(base_runs, cand_runs, thresholds)
    )
    return {
        "schema_version": 1,
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": status,
        "error": error_msg,
        "base_sha": target.base_sha or "unknown",
        "cand_sha": target.cand_sha or "unknown",
        "base_hinoki_file": (
            base_runs[0].get("hinoki_file", "unknown") if base_runs else "unknown"
        ),
        "cand_hinoki_file": (
            cand_runs[0].get("hinoki_file", "unknown") if cand_runs else "unknown"
        ),
        "env": cand_runs[0].get("env", {}) if cand_runs else {},
        "config": {
            "rounds": cfg.rounds,
            "seconds": cfg.seconds,
            "warmup": cfg.warmup,
            "connections": cfg.connections,
            "cand_slowdown_ms": cfg.cand_slowdown_ms,
            "informational": cfg.informational,
            "schedule": schedule,
            "thresholds": {
                "rps_regression_pct": thresholds.rps_regression_pct,
                "p95_regression_pct": thresholds.p95_regression_pct,
                "noise_cv_pct": thresholds.noise_cv_pct,
            },
        },
        "workloads": workloads,
        "raw_rounds": {"base": base_runs, "cand": cand_runs},
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Hinoki paired CI benchmark comparator"
    )
    parser.add_argument("--base-python", type=Path, required=True)
    parser.add_argument("--cand-python", type=Path, required=True)
    parser.add_argument(
        "--bench-script", type=Path, default=Path(__file__).with_name("ci_bench.py")
    )
    parser.add_argument("--base-sha", default="")
    parser.add_argument("--cand-sha", default="")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--seconds", type=float, default=2.0)
    parser.add_argument("--warmup", type=float, default=0.5)
    parser.add_argument("--connections", type=int, default=16)
    parser.add_argument("--cand-slowdown-ms", type=float, default=0.0)
    parser.add_argument("--rps-regression-pct", type=float, default=10.0)
    parser.add_argument("--p95-regression-pct", type=float, default=15.0)
    parser.add_argument("--noise-cv-pct", type=float, default=15.0)
    parser.add_argument("--out-json", type=Path, default=None)
    parser.add_argument("--out-md", type=Path, default=None)
    parser.add_argument("--informational", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    cfg = RunConfig(
        rounds=args.rounds,
        seconds=args.seconds,
        warmup=args.warmup,
        connections=args.connections,
        cand_slowdown_ms=args.cand_slowdown_ms,
        informational=args.informational,
    )
    thresholds = Thresholds(
        rps_regression_pct=args.rps_regression_pct,
        p95_regression_pct=args.p95_regression_pct,
        noise_cv_pct=args.noise_cv_pct,
    )
    target = CompareTarget(
        base_python=args.base_python,
        cand_python=args.cand_python,
        bench_script=args.bench_script,
        base_sha=args.base_sha,
        cand_sha=args.cand_sha,
    )
    report = run_comparison(target, cfg, thresholds)
    _write_outputs(report, args.out_json, args.out_md)
    if not cfg.informational and report["status"] in ("invalid", "regression"):
        sys.exit(1)


if __name__ == "__main__":
    main()
