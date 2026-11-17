"""Generate the benchmark tables from retained oha JSON."""

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


def load_runs(directory: Path, stack: str, scenario: str) -> list[dict]:
    files = sorted(directory.glob(f"{stack}-{scenario}-*.json"))
    if not files:
        raise ValueError(f"missing raw results for {stack} / {scenario} in {directory}")
    return [json.loads(path.read_text()) for path in files]


PERCENTILES = ("p50", "p95", "p99")


def spread(values: list[float], scale: float = 1.0) -> dict:
    return {
        "median": statistics.median(values) * scale,
        "low": min(values) * scale,
        "high": max(values) * scale,
    }


def run_summary(runs: list[dict], expected_status: str) -> dict:
    """Aggregate per-run results: medians of the per-run values plus min-max."""
    rps = [float(run["summary"]["requestsPerSec"]) for run in runs]
    mean_rps = statistics.mean(rps)
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
        "cv_pct": statistics.stdev(rps) / mean_rps * 100 if len(rps) > 1 else None,
        "latency_ms": {
            name: spread([run["latencyPercentiles"][name] for run in runs], 1000)
            for name in PERCENTILES
        },
        "wrong": wrong,
    }


def fmt_ms(value: float) -> str:
    return f"{value:,.0f}" if value >= 100 else f"{value:.1f}"


def machine_lines(env: dict) -> list[str]:
    settings = env["settings"]
    host = env.get("host", {})
    lines = [
        f"Run on {env['date']}. {env['cpu']}; {env['cores']} logical CPUs; {env['machine']}.",
    ]
    if host.get("performance_cores"):
        lines.append(
            f"{host['performance_cores']} performance and {host['efficiency_cores']} efficiency cores; "
            f"power: {host.get('power', 'unknown').splitlines()[0]}. PostgreSQL ran in a Docker Desktop virtual machine "
            f"({host.get('docker_vm', 'size not recorded')}), so every query crossed the VM boundary."
        )
    lines.append(
        f"Load average at start: {', '.join(f'{value:.2f}' for value in env['loadavg_at_start'])}."
    )
    if settings.get("server_cpus"):
        placement = (
            f"{settings['workers']} server processes on CPUs {settings['server_cpus']}; "
            f"load generator on CPUs {settings['load_cpus']}"
        )
    else:
        placement = (
            f"{settings['workers']} server processes; no CPU pinning "
            "(the operating system scheduled the servers and the load generator)"
        )
    lines.append(
        f"{placement}; {settings['concurrency']} concurrent connections; {settings['warmup_s']} s warm-up; "
        f"{settings['runs']} measured runs of {settings['duration_s']} s; {env['postgres']}; {env['oha']}."
    )
    return [" ".join(lines[:1]), *lines[1:], ""]


def stack_names(directory: Path, settings: dict) -> list[str]:
    stacks = settings.get("stacks") or [
        name for name in FRAMEWORKS if any(directory.glob(f"{name}-health-*.json"))
    ]
    if not stacks:
        raise ValueError(f"no benchmark stacks listed or found in {directory}")
    for stack in stacks:
        if stack not in FRAMEWORKS:
            raise ValueError(f"unknown stack {stack!r} in {directory}")
        if not (directory / f"{stack}-verify.txt").exists():
            raise ValueError(f"missing verification output for {stack} in {directory}")
    return stacks


def cpu_seconds(text: str) -> float:
    """Parse `ps` CPU time: `mm:ss.ss` (macOS) or `[dd-]hh:mm:ss` (Linux)."""
    days, _, clock = text.rpartition("-")
    *larger, seconds = clock.split(":")
    total = float(seconds)
    for unit, part in zip((60, 3600), reversed(larger), strict=False):
        total += int(part) * unit
    return total + (int(days) * 86400 if days else 0)


def busy_share(directory: Path, stack: str, workers: int) -> float | None:
    """Share of CPU time held by the busiest of the `workers` busiest processes."""
    path = directory / f"{stack}-workers.txt"
    if not path.exists():
        return None
    times = sorted(
        (cpu_seconds(line.split()[2]) for line in path.read_text().splitlines()[1:]),
        reverse=True,
    )[:workers]
    total = sum(times)
    return times[0] / total if workers > 1 and len(times) > 1 and total else None


def single_process_stacks(
    directory: Path, stacks: list[str], workers: int
) -> list[str]:
    """Stacks where one process did nearly all the work despite several being started."""
    shares = {stack: busy_share(directory, stack, workers) for stack in stacks}
    return [stack for stack, share in shares.items() if share and share > 0.8]


STABLE_CV = 15


def slug(directory: Path) -> str:
    return "-".join(directory.parts[-2:]).lower().replace("_", "-")


def unstable(summaries: list[tuple[str, dict]]) -> bool:
    return any(row["cv_pct"] and row["cv_pct"] > STABLE_CV for _, row in summaries)


def result_row(scenario: str, label: str, row: dict) -> str:
    latency = row["latency_ms"]
    cv = "n/a" if row["cv_pct"] is None else f"{row['cv_pct']:.1f}%"
    return (
        f'<tr><th scope="row">{scenario.title()}</th><th scope="row">{html.escape(label)}</th>'
        f'<td>{row["median"]:,.0f}<span class="range">{row["low"]:,.0f}&ndash;{row["high"]:,.0f}</span></td>'
        f"<td>{fmt_ms(latency['p50']['median'])}</td><td>{fmt_ms(latency['p99']['median'])}</td>"
        f"<td>{cv}</td><td>{row['wrong']}</td><td>{row['runs']}</td></tr>"
    )


def throughput_panel(scenario: str, rows: list[tuple[str, dict]]) -> str:
    peak = max(row["high"] for _, row in rows)
    bars = "".join(
        f'<div class="bench-row"><span class="bench-name">{html.escape(label)}</span>'
        '<span class="bench-track">'
        f'<span class="bench-bar" style="width:{row["median"] / peak * 100:.2f}%"></span>'
        f'<span class="bench-whisker" style="left:{row["low"] / peak * 100:.2f}%;'
        f'width:{(row["high"] - row["low"]) / peak * 100:.2f}%"></span></span>'
        f'<span class="bench-value">{row["median"]:,.0f}</span></div>'
        for label, row in rows
    )
    return f'<div class="bench-panel"><div class="bench-title">{scenario.title()}</div>{bars}</div>'


def throughput_chart(reported: dict[str, list[tuple[str, dict]]], chart_id: str) -> str:
    panels = "".join(throughput_panel(name, rows) for name, rows in reported.items())
    description = "; ".join(
        f"{name}: " + ", ".join(f"{label} {row['median']:,.0f}" for label, row in rows)
        for name, rows in reported.items()
    )
    return (
        '<figure class="bench-figure">'
        f'<p id="{chart_id}-desc" class="bench-sr">Median requests per second by stack, '
        f"one panel per scenario, each with its own scale, higher is better. {html.escape(description)}.</p>"
        f'<div class="bench-chart" role="img" aria-describedby="{chart_id}-desc">{panels}</div>'
        "<figcaption>Median requests per second (higher is better). Each panel has its own scale from 0; "
        "the line spans the lowest to highest run. The table below has the full numbers.</figcaption>"
        "</figure>"
    )


def results_table(reported: dict[str, list[tuple[str, dict]]]) -> str:
    rows_html = "".join(
        result_row(scenario, label, row)
        for scenario, rows in reported.items()
        for label, row in rows
    )
    return (
        '<div class="benchmark-table-wrap"><table class="benchmark-table">'
        "<caption>Median of runs, with the lowest&ndash;highest run under requests per second</caption>"
        '<thead><tr><th scope="col">Scenario</th><th scope="col">Stack</th><th scope="col">Requests/s</th>'
        '<th scope="col">p50 ms</th><th scope="col">p99 ms</th><th scope="col">CV</th>'
        '<th scope="col">Unexpected</th><th scope="col">Runs</th></tr></thead>'
        f"<tbody>{rows_html}</tbody></table></div>"
    )


def render_mode(directory: Path) -> str:
    env_path = directory / "environment.json"
    if not env_path.exists():
        raise ValueError(f"missing environment metadata in {directory}")
    env = json.loads(env_path.read_text())
    settings = env["settings"]
    mode = settings.get("server_mode", "documented")
    stacks = stack_names(directory, settings)
    single = single_process_stacks(directory, stacks, settings["workers"])
    stacks = [stack for stack in stacks if stack not in single]
    lines = [
        f"## {mode.replace('-', ' ').title()} server slice",
        "",
        *machine_lines(env),
    ]
    for stack in single:
        lines.extend(
            [
                f"{FRAMEWORKS[stack]} is not reported here: it was started with {settings['workers']} processes, "
                f"but its per-process CPU time (`{stack}-workers.txt`) shows one process did nearly all the work, "
                "so it is not a comparable result. Its raw samples are retained.",
                "",
            ]
        )
    reported: dict[str, list[tuple[str, dict]]] = {}
    withheld = []
    for scenario in SCENARIOS:
        summaries = [
            (
                FRAMEWORKS[stack],
                run_summary(load_runs(directory, stack, scenario), EXPECTED[scenario]),
            )
            for stack in stacks
        ]
        if unstable(summaries):
            withheld.append(scenario)
        else:
            reported[scenario] = summaries
    if mode == "documented":
        lines.extend([throughput_chart(reported, f"bench-{slug(directory)}"), ""])
    lines.extend([results_table(reported), ""])
    if withheld:
        lines.extend(
            [
                f"Not reported: {', '.join(withheld)}. The requests-per-second CV exceeded {STABLE_CV}% for at least one stack, "
                "so the samples are too unstable to compare; the raw results are retained.",
                "",
            ]
        )
    lines.extend(
        [
            "All listed stacks passed the correctness and SQL statement-count verifier before measurement. "
            "CV is the coefficient of variation of requests per second across runs. "
            "Requests in flight when a sample ended were aborted by `oha` and are not counted as unexpected.",
            "",
        ]
    )
    return "\n".join(lines)


def main(directories: list[Path]) -> None:
    print("---\nstatus: implemented\n---\n")
    print("# Benchmarks\n")
    print(
        "These measurements describe the recorded implementations, machines and workload only. They are not a universal comparison or a production-readiness claim. "
        "Each row gives requests per second and the p50 and p99 response time in milliseconds. "
        "`health` is diagnostic; the database-backed scenarios are the primary ones. No overall winner is calculated. "
        "A scenario whose samples are too unstable to compare is listed as not reported.\n"
    )
    for directory in directories:
        print(render_mode(directory))
    locations = ", ".join(
        sorted(
            {f"`benchmarks/comparative/results/{d.parent.name}/`" for d in directories}
        )
    )
    print(
        "## Method and limits\n\n"
        "Each stack used the same PostgreSQL seed and request/response oracle. The verifier checked response content, status and SQL statement counts before measurement. Create uses per-statement autocommit in each implementation. The documented-stack and same-Granian ASGI slices answer different questions: Bolt stays on its own server and is not included in the ASGI same-server control, so differences in the documented slice may come from the server as well as the framework.\n\n"
        "Latency is the time `oha` measured from sending a request to receiving the full response, over loopback, with a fixed number of concurrent connections that send the next request as soon as one finishes. Latency and throughput are therefore linked (at fixed concurrency, higher latency means fewer requests per second) and are not independent evidence. "
        "Each percentile shown is the median of that percentile across the individual runs, with the lowest and highest run as the range; it is not a percentile of all requests pooled together. A min-to-max range is descriptive, not a confidence interval.\n\n"
        "Each section is one host and one run window, and none was taken on an idle host: the load average at the start of each run is listed in its section. Drivers and ORM scheduling differ; equal pool settings do not imply identical behavior. The dataset is small and warm and this does not measure TLS, proxies, authentication, compression, memory, cold data or other workloads. "
        f"Raw JSON, environment metadata, server logs, per-worker CPU time and verification output are retained in {locations}; reproduction instructions are in `benchmarks/comparative/README.md`. Inspect them before drawing conclusions."
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: report.py RESULTS_DIR [RESULTS_DIR ...]")
    main([Path(arg) for arg in sys.argv[1:]])
