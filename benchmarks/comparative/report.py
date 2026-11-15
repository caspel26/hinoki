"""Generate benchmark charts and accessible tables from retained oha JSON."""

import html
import json
import math
import re
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


def nice_ceiling(value: float) -> float:
    """Smallest 1/2/2.5/5/10 x 10^k at or above value, so four ticks read cleanly."""
    magnitude = 10 ** math.floor(math.log10(value))
    return next(
        step * magnitude for step in (1, 2, 2.5, 5, 10) if step * magnitude >= value
    )


def slug(directory: Path) -> str:
    return re.sub(r"[^a-z0-9]+", "-", "-".join(directory.parts[-2:]).lower()).strip("-")


def latency_description(rows: list[tuple[str, dict]]) -> str:
    return "; ".join(
        f"{label}: "
        + ", ".join(
            f"{name} {fmt_ms(row['latency_ms'][name]['median'])} ms "
            f"(runs {fmt_ms(row['latency_ms'][name]['low'])} to {fmt_ms(row['latency_ms'][name]['high'])})"
            for name in PERCENTILES
        )
        for label, row in rows
    )


def percent_of(value: float, ceiling: float) -> float:
    return min(value / ceiling * 100, 100)


def latency_bar_row(name: str, stat: dict, ceiling: float) -> str:
    low = percent_of(stat["low"], ceiling)
    high = percent_of(stat["high"], ceiling)
    return (
        f'<div class="lat-row"><span class="lat-p">{name}</span>'
        f'<span class="lat-track"><span class="lat-bar lat-{name}" style="width:{percent_of(stat["median"], ceiling):.2f}%"></span>'
        f'<span class="lat-whisker" style="left:{low:.2f}%;width:{high - low:.2f}%"></span></span>'
        f'<span class="lat-val">{fmt_ms(stat["median"])} ms</span></div>'
    )


def latency_chart(scenario: str, rows: list[tuple[str, dict]], chart_id: str) -> str:
    peak = max(
        row["latency_ms"][name]["high"] for _, row in rows for name in PERCENTILES
    )
    ceiling = nice_ceiling(peak)
    ticks = "".join(
        f'<span style="left:{step * 50}%">{ceiling * step / 2:,g}</span>'
        for step in range(3)
    )
    legend = "".join(
        f'<span class="lat-key"><span class="lat-swatch lat-{name}"></span>{name}</span>'
        for name in PERCENTILES
    )
    groups = "".join(
        f'<div class="lat-group"><div class="lat-name">{html.escape(label)}</div>'
        + "".join(
            latency_bar_row(name, row["latency_ms"][name], ceiling)
            for name in PERCENTILES
        )
        + "</div>"
        for label, row in rows
    )
    return (
        f'<p id="{chart_id}-title" class="benchmark-sr">{html.escape(scenario.title())} response latency in milliseconds, lower is better.</p>'
        f'<p id="{chart_id}-desc" class="benchmark-sr">Median across runs of each run\'s percentile, with the lowest to highest run. '
        f"{html.escape(latency_description(rows))}</p>"
        f'<div class="lat-legend" aria-hidden="true">{legend}'
        f'<span class="lat-key lat-note">Bar = median of runs · line = lowest to highest run</span></div>'
        f'<div class="lat-chart" role="img" aria-labelledby="{chart_id}-title" aria-describedby="{chart_id}-desc">'
        f'{groups}<div class="lat-scale"><span class="lat-p"></span><span class="lat-ticks">{ticks}</span><span class="lat-val"></span></div>'
        f'<div class="lat-unit">Response time (ms), linear axis from 0 &middot; lower is better</div></div>'
    )


def additional_capture_lines(directory: Path) -> list[str]:
    captures = []
    for capture_path in sorted(directory.glob("environment-*.json")):
        capture = json.loads(capture_path.read_text())
        settings = capture["settings"]
        captures.append(
            f"Additional capture {capture_path.name}: {capture['date']}; "
            f"load average {', '.join(f'{value:.2f}' for value in capture['loadavg_at_start'])}; "
            f"stacks {', '.join(settings['stacks'])}; "
            f"{settings['runs']} runs x {settings['duration_s']} s for "
            f"{', '.join(settings.get('scenarios', SCENARIOS))}."
        )
    return captures + ([""] if captures else [])


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


def latency_cell(stat: dict) -> str:
    return (
        f"<td>{fmt_ms(stat['median'])}"
        f'<span class="range">{fmt_ms(stat["low"])}&ndash;{fmt_ms(stat["high"])}</span></td>'
    )


def latency_table(scenario: str, summaries: list[tuple[str, dict]]) -> str:
    rows_html = "".join(
        f'<tr><th scope="row">{html.escape(label)}</th>'
        + "".join(latency_cell(row["latency_ms"][name]) for name in PERCENTILES)
        + f"<td>{row['wrong']}</td><td>{row['runs']}</td></tr>"
        for label, row in summaries
    )
    return (
        '<div class="benchmark-table-wrap"><table class="benchmark-table">'
        f"<caption>{scenario.title()} response time in milliseconds: median of runs, lowest&ndash;highest run below</caption>"
        '<thead><tr><th scope="col">Stack</th><th scope="col">p50 ms</th><th scope="col">p95 ms</th>'
        '<th scope="col">p99 ms</th><th scope="col">Unexpected responses</th><th scope="col">Runs</th></tr></thead>'
        f"<tbody>{rows_html}</tbody></table></div>"
    )


def throughput_table(summaries: dict[str, list[tuple[str, dict]]]) -> str:
    rows_html = "".join(
        f'<tr><th scope="row">{scenario.title()}</th><th scope="row">{html.escape(label)}</th>'
        f"<td>{row['median']:,.0f}</td><td>{row['low']:,.0f}&ndash;{row['high']:,.0f}</td>"
        f"<td>{cv_cell(row['cv_pct'])}</td></tr>"
        for scenario, rows in summaries.items()
        for label, row in rows
    )
    return (
        '<div class="benchmark-table-wrap"><table class="benchmark-table">'
        "<caption>Requests per second, secondary metric: median of runs, lowest&ndash;highest run, coefficient of variation</caption>"
        '<thead><tr><th scope="col">Scenario</th><th scope="col">Stack</th><th scope="col">Median req/s</th>'
        '<th scope="col">Range req/s</th><th scope="col">CV</th></tr></thead>'
        f"<tbody>{rows_html}</tbody></table></div>"
    )


def cv_cell(cv_pct: float | None) -> str:
    if cv_pct is None:
        return "n/a"
    return f"{cv_pct:.1f}% (above 15%)" if cv_pct > 15 else f"{cv_pct:.1f}%"


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
    minutes, seconds = text.split(":")
    return int(minutes) * 60 + float(seconds)


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
        f"## {mode.replace('-', ' ').title()} server slice: {env['cpu']}",
        "",
        *machine_lines(env),
    ]
    lines.extend(additional_capture_lines(directory))
    for stack in single:
        lines.extend(
            [
                f"{FRAMEWORKS[stack]} is not charted here: it was started with {settings['workers']} processes, "
                "but its per-process CPU time (`"
                + f"{stack}-workers.txt"
                + "`) shows one process did nearly all the work. "
                f"That is not a {settings['workers']}-process result and is not comparable with the other stacks; "
                "its raw samples are retained in the results directory.",
                "",
            ]
        )
    by_scenario = {
        scenario: [
            (
                FRAMEWORKS[stack],
                run_summary(load_runs(directory, stack, scenario), EXPECTED[scenario]),
            )
            for stack in stacks
        ]
        for scenario in SCENARIOS
    }
    for scenario, summaries in by_scenario.items():
        chart_id = f"benchmark-{slug(directory)}-{scenario}"
        lines.extend(
            [
                f"### {scenario.title()} response time",
                "",
                '<div class="benchmark-result">',
                '<figure class="benchmark-figure">',
                latency_chart(scenario, summaries, chart_id),
                "<figcaption>Response latency in milliseconds (lower is better). Each bar is the median across runs of that run's percentile; the line spans the lowest to highest run.</figcaption>",
                "</figure>",
                latency_table(scenario, summaries),
                "</div>",
                "",
            ]
        )
    lines.extend(
        [
            "### Throughput (secondary)",
            "",
            throughput_table(by_scenario),
            "",
            "Requests in flight when a sample ended were aborted by `oha`; these deadline aborts are excluded from unexpected-error totals. All listed stacks passed the correctness and SQL statement-count verifier before measurement. CV is the sample coefficient of variation across requests/second samples; values above 15% are a signal to investigate host variance and repeat when justified, not proof that the run is invalid.",
            "",
        ]
    )
    return "\n".join(lines)


def slice_label(directory: Path) -> str:
    env = json.loads((directory / "environment.json").read_text())
    mode = env["settings"].get("server_mode", "documented").replace("-", " ")
    return f"the {mode} slice on {env['cpu']}"


def noisy_create_slices(directories: list[Path]) -> list[Path]:
    noisy = []
    for directory in directories:
        stacks = json.loads((directory / "environment.json").read_text())["settings"][
            "stacks"
        ]
        summaries = [
            run_summary(load_runs(directory, stack, "create"), EXPECTED["create"])
            for stack in stacks
        ]
        if any(summary["cv_pct"] and summary["cv_pct"] > 15 for summary in summaries):
            noisy.append(directory)
    return noisy


def main(directories: list[Path]) -> None:
    print("---\nstatus: implemented\n---\n")
    print("# Benchmarks\n")
    print(
        "These measurements describe the recorded implementations, machines and workload only. They are not a universal comparison or a production-readiness claim. "
        "The primary metric is HTTP response time (p50, p95 and p99, in milliseconds, lower is better); requests per second is a secondary table in each slice. "
        "`health` is diagnostic; database-backed list, detail and create are the primary scenarios. No overall winner is calculated. "
        "Runs from different machines are shown in separate sections and must not be compared with each other.\n"
    )
    for directory in directories:
        print(render_mode(directory))
    noisy = noisy_create_slices(directories)
    if noisy:
        names = ", ".join(slice_label(directory) for directory in noisy)
        print(
            "## Interpretation\n\n"
            f"Create varied by more than 15% (coefficient of variation of requests per second) in: {names}. "
            "Treat write measurements in those slices, including their latency percentiles, as inconclusive for close comparisons; "
            "the median and range are descriptive and do not establish a winner.\n"
        )
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
