"""Generate benchmark charts and accessible tables from retained oha JSON."""

import html
import json
import statistics
import sys
from pathlib import Path

FRAMEWORKS = {
    "hinoki": "Hinoki · Granian",
    "fastapi": "FastAPI · Uvicorn",
    "ninja": "Django Ninja · Uvicorn",
    "bolt": "Django Bolt · Actix",
    "hinoki-granian": "Hinoki · Granian",
    "fastapi-granian": "FastAPI · Granian",
    "ninja-granian": "Django Ninja · Granian",
}
SCENARIOS = ["health", "list", "detail", "create"]
EXPECTED = {"health": "200", "list": "200", "detail": "200", "create": "201"}
COLORS = ["var(--hn-brand-fill)", "var(--hn-info)", "var(--hn-green)", "var(--hn-wood)"]


def load_runs(directory: Path, stack: str, scenario: str) -> list[dict]:
    files = sorted(directory.glob(f"{stack}-{scenario}-*.json"))
    if not files:
        raise ValueError(f"missing raw results for {stack} / {scenario} in {directory}")
    return [json.loads(path.read_text()) for path in files]


def run_summary(runs: list[dict], expected_status: str) -> dict:
    rps = [float(run["summary"]["requestsPerSec"]) for run in runs]
    wrong = sum(
        count
        for run in runs
        for code, count in run["statusCodeDistribution"].items()
        if code != expected_status
    ) + sum(
        count
        for run in runs
        for kind, count in run["errorDistribution"].items()
        if kind != "aborted due to deadline"
    )
    return {
        "runs": len(runs),
        "median": statistics.median(rps),
        "low": min(rps),
        "high": max(rps),
        "p50": statistics.median(run["latencyPercentiles"]["p50"] for run in runs),
        "p95": statistics.median(run["latencyPercentiles"]["p95"] for run in runs),
        "p99": statistics.median(run["latencyPercentiles"]["p99"] for run in runs),
        "wrong": wrong,
    }


def svg_chart(scenario: str, rows: list[tuple[str, dict]], chart_id: str) -> str:
    width, height = 840, 340
    left, right, top, bottom = 66, 24, 26, 78
    plot_h = height - top - bottom
    maximum = max((row["high"] for _, row in rows), default=1) or 1
    ceiling = maximum * 1.12
    bars = []
    ticks = []
    for step in range(5):
        value = ceiling * step / 4
        y = top + plot_h * (1 - step / 4)
        ticks.append(
            f'<line class="bench-gridline" x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" />'
            f'<text class="bench-axis" x="{left - 10}" y="{y + 4:.1f}" text-anchor="end">{value:,.0f}</text>'
        )
    slot = (width - left - right) / len(rows)
    bar_width = min(56, slot * 0.42)
    for index, (label, row) in enumerate(rows):
        center = left + slot * (index + 0.5)
        y_value = top + plot_h * (1 - row["median"] / ceiling)
        y_high = top + plot_h * (1 - row["high"] / ceiling)
        y_low = top + plot_h * (1 - row["low"] / ceiling)
        color = COLORS[index % len(COLORS)]
        bars.append(
            f'<rect class="bench-bar" x="{center - bar_width / 2:.1f}" y="{y_value:.1f}" '
            f'width="{bar_width:.1f}" height="{top + plot_h - y_value:.1f}" rx="4" style="fill:{color}" />'
            f'<line class="bench-range" x1="{center:.1f}" y1="{y_high:.1f}" x2="{center:.1f}" y2="{y_low:.1f}" />'
            f'<line class="bench-range" x1="{center - 7:.1f}" y1="{y_high:.1f}" x2="{center + 7:.1f}" y2="{y_high:.1f}" />'
            f'<line class="bench-range" x1="{center - 7:.1f}" y1="{y_low:.1f}" x2="{center + 7:.1f}" y2="{y_low:.1f}" />'
            f'<text class="bench-axis" x="{center:.1f}" y="{height - 46}" text-anchor="middle">{html.escape(label)}</text>'
            f'<text class="bench-value" x="{center:.1f}" y="{height - 23}" text-anchor="middle">{row["median"]:,.0f}</text>'
        )
    description = "; ".join(
        f"{label}: median {row['median']:,.0f}, range {row['low']:,.0f} to {row['high']:,.0f} requests per second"
        for label, row in rows
    )
    return (
        f'<svg class="benchmark-chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-labelledby="{chart_id}-title {chart_id}-desc">'
        f'<title id="{chart_id}-title">{html.escape(scenario.title())} requests per second</title>'
        f'<desc id="{chart_id}-desc">Median requests per second with min-to-max range across runs. {html.escape(description)}</desc>'
        f"<style>.benchmark-chart{{color:var(--hn-text);font-family:var(--hn-font-sans, sans-serif)}}"
        f".bench-gridline{{stroke:var(--hn-edge);stroke-width:1}}.bench-axis{{fill:var(--hn-text-2);font-size:12px}}"
        f".bench-value{{fill:var(--hn-text);font-size:12px;font-weight:600}}.bench-range{{stroke:var(--hn-text);stroke-width:2}}"
        f"</style>{''.join(ticks)}{''.join(bars)}</svg>"
    )


def render_mode(directory: Path) -> str:
    env_path = directory / "environment.json"
    if not env_path.exists():
        raise ValueError(f"missing environment metadata in {directory}")
    env = json.loads(env_path.read_text())
    settings = env["settings"]
    mode = settings.get("server_mode", "documented")
    stacks = settings.get("stacks", [])
    if not stacks:
        stacks = [
            name for name in FRAMEWORKS if any(directory.glob(f"{name}-health-*.json"))
        ]
    if not stacks:
        raise ValueError(f"no benchmark stacks listed or found in {directory}")
    for stack in stacks:
        if stack not in FRAMEWORKS:
            raise ValueError(f"unknown stack {stack!r} in {directory}")
        if not (directory / f"{stack}-verify.txt").exists():
            raise ValueError(f"missing verification output for {stack} in {directory}")
    lines = [
        f"## {mode.replace('-', ' ').title()} server slice",
        "",
        f"Run on {env['date']}. {env['cpu']}; {env['cores']} logical CPUs; {env['machine']}.",
        f"Load average at start: {', '.join(f'{value:.2f}' for value in env['loadavg_at_start'])}.",
        f"{settings['workers']} server processes on CPUs {settings['server_cpus']}; load generator on CPUs {settings['load_cpus']}; "
        f"{settings['concurrency']} concurrent connections; {settings['warmup_s']} s warm-up; "
        f"{settings['runs']} measured runs of {settings['duration_s']} s; {env['postgres']}; {env['oha']}.",
        "",
    ]
    for scenario in SCENARIOS:
        summaries = [
            (
                FRAMEWORKS[stack],
                run_summary(load_runs(directory, stack, scenario), EXPECTED[scenario]),
            )
            for stack in stacks
        ]
        chart_id = f"benchmark-{mode}-{scenario}"
        rows_html = "".join(
            f'<tr><th scope="row">{html.escape(label)}</th>'
            f"<td>{row['median']:,.0f}</td><td>{row['low']:,.0f}&ndash;{row['high']:,.0f}</td>"
            f"<td>{row['p50'] * 1000:.1f}</td><td>{row['p95'] * 1000:.1f}</td><td>{row['p99'] * 1000:.1f}</td>"
            f"<td>{row['wrong']}</td><td>{row['runs']}</td></tr>"
            for label, row in summaries
        )
        lines.extend(
            [
                f"### {scenario.title()}",
                "",
                '<div class="benchmark-result">',
                '<figure class="benchmark-figure">',
                svg_chart(scenario, summaries, chart_id),
                "<figcaption>Requests per second: median bar and observed min-to-max whisker across runs. Values and latency percentiles use the same raw JSON files.</figcaption>",
                "</figure>",
                '<div class="benchmark-table-wrap">',
                '<table class="benchmark-table">',
                f"<caption>{scenario.title()} results across {len(summaries)} stacks</caption>",
                '<thead><tr><th scope="col">Stack</th><th scope="col">Median req/s</th><th scope="col">Range req/s</th>'
                '<th scope="col">p50 ms</th><th scope="col">p95 ms</th><th scope="col">p99 ms</th>'
                '<th scope="col">Unexpected responses</th><th scope="col">Runs</th></tr></thead>',
                f"<tbody>{rows_html}</tbody>",
                "</table></div></div>",
                "",
            ]
        )
    lines.extend(
        [
            "Requests in flight when a sample ended were aborted by `oha`; these deadline aborts are excluded from unexpected-error totals. All listed stacks passed the correctness and SQL statement-count verifier before measurement.",
            "",
        ]
    )
    return "\n".join(lines)


def main(directories: list[Path]) -> None:
    print("# Measured results\n")
    print(
        "These measurements describe the recorded implementations, machine and workload only. They are not a universal comparison or a production-readiness claim. `health` is diagnostic; database-backed list, detail and create are the primary scenarios. No overall winner is calculated.\n"
    )
    for directory in directories:
        print(render_mode(directory))
    print(
        "## Method and limits\n\n"
        "Each stack used the same PostgreSQL seed and request/response oracle. The verifier checked response content, status and SQL statement counts before measurement. Create uses per-statement autocommit in each implementation. The documented-stack and same-Granian ASGI slices answer different questions: Bolt stays on its own server and is not included in the ASGI same-server control.\n\n"
        "This is one host and one run window. Drivers and ORM scheduling differ; equal pool settings do not imply identical behavior. The dataset is small and warm, networking is loopback, and this does not measure TLS, proxies, authentication, compression, memory, cold data or other workloads. A min-to-max range is descriptive, not a confidence interval. Inspect retained raw JSON, environment metadata, server logs and verification output before drawing conclusions.\n"
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: report.py RESULTS_DIR [RESULTS_DIR ...]")
    main([Path(arg) for arg in sys.argv[1:]])
