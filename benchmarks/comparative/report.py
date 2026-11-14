"""Turn the raw oha results of a run into one readable Markdown report.

uv run python report.py results/2026-11-14 > results/2026-11-14/report.md
"""

import json
import statistics
import sys
from pathlib import Path

FRAMEWORKS = ["hinoki", "fastapi", "ninja", "bolt"]
LABELS = {
    "hinoki": "Hinoki",
    "fastapi": "FastAPI",
    "ninja": "Django Ninja",
    "bolt": "Django Bolt",
}
SCENARIOS = ["health", "list", "detail", "create"]
DEADLINE = (
    "aborted due to deadline"  # requests in flight when a run ends (one per connection)
)
EXPECTED = {"health": "200", "list": "200", "detail": "200", "create": "201"}


def load(out: Path, framework: str, scenario: str) -> list[dict]:
    return [
        json.loads(p.read_text())
        for p in sorted(out.glob(f"{framework}-{scenario}-*.json"))
    ]


def ms(seconds: float) -> str:
    return f"{seconds * 1000:.1f}"


def main(out: Path) -> None:
    env = json.loads((out / "environment.json").read_text())
    settings = env["settings"]
    print("# Measured results\n")
    print(
        f"Run on {env['date']}. {env['cpu']}, {env['cores']} logical CPUs, {env['machine']}."
    )
    print(
        f"Load average when the run started: {', '.join(f'{x:.2f}' for x in env['loadavg_at_start'])}."
    )
    print(
        f"{settings['workers']} server processes pinned to CPUs {settings['server_cpus']}, load generator on CPUs "
        f"{settings['load_cpus']}, {settings['concurrency']} concurrent connections, {settings['warmup_s']} s warm-up, "
        f"{settings['runs']} measured runs of {settings['duration_s']} s per scenario. {env['postgres']}; {env['oha']}.\n"
    )
    for scenario in SCENARIOS:
        print(f"## {scenario}\n")
        print(
            "| Framework | Requests/s (median of runs) | Range across runs | p50 ms | p95 ms | p99 ms | Responses not `"
            + EXPECTED[scenario]
            + "` |"
        )
        print("| --- | ---: | --- | ---: | ---: | ---: | ---: |")
        for framework in FRAMEWORKS:
            runs = load(out, framework, scenario)
            if not runs:
                continue
            rps = [r["summary"]["requestsPerSec"] for r in runs]
            wrong = sum(
                count
                for r in runs
                for code, count in r["statusCodeDistribution"].items()
                if code != EXPECTED[scenario]
            ) + sum(
                count
                for r in runs
                for kind, count in r["errorDistribution"].items()
                if kind != DEADLINE
            )

            def pct(key: str, runs: list[dict] = runs) -> str:
                return ms(statistics.median(r["latencyPercentiles"][key] for r in runs))

            print(
                f"| {LABELS[framework]} | {statistics.median(rps):,.0f} | {min(rps):,.0f} to {max(rps):,.0f} "
                f"| {pct('p50')} | {pct('p95')} | {pct('p99')} | {wrong} |"
            )
        print()
    print(
        "Requests still in flight when a run ends are aborted by the load generator (64 per run) and are not counted as errors.\n"
    )
    print("## Statements per request (from the verification step)\n")
    for framework in FRAMEWORKS:
        text = out / f"{framework}-verify.txt"
        if text.exists():
            print(
                f"### {LABELS[framework]}\n\n```text\n{text.read_text().strip()}\n```\n"
            )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
