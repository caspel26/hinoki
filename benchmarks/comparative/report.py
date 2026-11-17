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


def result_row(label: str, row: dict, lead: str = "") -> str:
    latency = row["latency_ms"]
    cv = "n/a" if row["cv_pct"] is None else f"{row['cv_pct']:.1f}%"
    return (
        f'<tr>{lead}<th scope="row">{html.escape(label)}</th>'
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
        result_row(
            label,
            row,
            f'<th scope="rowgroup" rowspan="{len(rows)}" class="bench-group">{scenario.title()}</th>'
            if position == 0
            else "",
        )
        for scenario, rows in reported.items()
        for position, (label, row) in enumerate(rows)
    )
    return (
        '<div class="benchmark-table-wrap"><table class="benchmark-table">'
        "<caption>Median of runs, with the lowest&ndash;highest run under requests per second</caption>"
        '<thead><tr><th scope="col">Scenario</th><th scope="col">Stack</th><th scope="col">Requests/s</th>'
        '<th scope="col">p50 ms</th><th scope="col">p99 ms</th><th scope="col">CV</th>'
        '<th scope="col">Unexpected</th><th scope="col">Runs</th></tr></thead>'
        f"<tbody>{rows_html}</tbody></table></div>"
    )


SERVICE_ROWS = (
    (
        "health",
        "GET /health",
        "Static JSON, no database: the cost of the framework alone.",
    ),
    (
        "list",
        "GET /posts?page=1&page_size=20",
        "Published posts with their author, newest first, in a page envelope with the total: one COUNT and one joined SELECT.",
    ),
    (
        "detail",
        "GET /posts/{id}",
        "One published post with its author: one joined SELECT.",
    ),
    (
        "create",
        "POST /posts",
        "Validate the body, insert, return the post with its author: one INSERT and one author SELECT.",
    ),
)
STACK_ROWS = (
    (
        "hinoki",
        "Hinoki",
        "Hinoki ORM (Rust, tokio-postgres)",
        "Granian (ASGI)",
        ("hinoki", "granian"),
    ),
    (
        "fastapi",
        "FastAPI",
        "SQLAlchemy async, asyncpg",
        "Uvicorn with uvloop",
        ("fastapi", "sqlalchemy", "asyncpg", "uvicorn"),
    ),
    (
        "ninja",
        "Django Ninja",
        "Django async ORM, psycopg 3 pool",
        "Uvicorn with uvloop",
        ("django-ninja", "django", "psycopg"),
    ),
    (
        "bolt",
        "Django Bolt",
        "Django async ORM, psycopg 3 pool",
        "its own Actix server",
        ("django-bolt",),
    ),
)
SLICE_TITLES = {
    "documented": (
        "Each stack on its own server",
        "Each framework runs on the server it documents: Hinoki on Granian, FastAPI and Django Ninja on Uvicorn, Django Bolt on its own Actix server. "
        "A difference here can come from the server as well as from the framework.",
    ),
    "same-granian": (
        "Same server for the ASGI stacks",
        "Hinoki, FastAPI and Django Ninja run on one Granian version and configuration, which takes the server out of the comparison. "
        "Django Bolt is not in this slice because it does not expose ASGI.",
    ),
}
READING_NOTES = (
    "**Requests per second and latency are one measurement.** Every run keeps 64 connections busy, so higher latency means fewer requests per second; the two do not confirm each other.",
    "**Each chart panel has its own scale from 0.** The scenarios differ by orders of magnitude, so bars compare stacks inside a panel, never across panels. The line over a bar is the lowest to highest run.",
    "**p50 and p99 are medians of per-run percentiles**, not percentiles of all requests pooled together. A range is descriptive, not a confidence interval.",
    f"**CV is the coefficient of variation of requests per second across runs.** Above {STABLE_CV}% for any stack, the scenario is not reported: the samples are too unstable to compare, and the raw files are kept.",
    "**What this does not cover:** TLS, proxies, authentication, compression, memory, cold data, larger tables or other hardware. Pool sizes are equal per process, but each library schedules work differently.",
)
REPRODUCE = """```sh
cd benchmarks/comparative
uv sync --locked
VIRTUAL_ENV=$PWD/.venv uvx maturin develop --release --uv --manifest-path ../../crates/bindings/Cargo.toml
uv run --locked python run.py --server-mode documented --out results/YYYY-MM-DD/documented
uv run --locked python run.py --server-mode same-granian --out results/YYYY-MM-DD/same-granian
uv run --locked python report.py results/YYYY-MM-DD/documented results/YYYY-MM-DD/same-granian > ../../docs/benchmarks.md
```"""


def package_versions(env: dict) -> dict[str, str]:
    packages = env.get("packages", [])
    if isinstance(packages, str):  # run.py stores `uv pip list` output as JSON text
        packages = json.loads(packages)
    return {item["name"]: item["version"] for item in packages}


def indented(text: str, spaces: int = 4) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line else line for line in text.split("\n"))


def render_mode(directory: Path) -> str:
    env_path = directory / "environment.json"
    if not env_path.exists():
        raise ValueError(f"missing environment metadata in {directory}")
    env = json.loads(env_path.read_text())
    settings = env["settings"]
    stacks = stack_names(directory, settings)
    single = single_process_stacks(directory, stacks, settings["workers"])
    stacks = [stack for stack in stacks if stack not in single]
    lines: list[str] = []
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
    lines.extend(
        [
            throughput_chart(reported, f"bench-{slug(directory)}"),
            "",
            results_table(reported),
            "",
        ]
    )
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
            "Requests in flight when a sample ended were aborted by `oha` and are not counted as unexpected.",
            "",
            '??? info "Environment of this run"',
            "",
            *(indented(line) for line in machine_lines(env)),
        ]
    )
    return "\n".join(lines)


def service_table() -> str:
    rows = "\n".join(
        f"| `{name}` | `{request}` | {what} |" for name, request, what in SERVICE_ROWS
    )
    return (
        "| Scenario | Request | What the service does |\n| --- | --- | --- |\n" + rows
    )


def stacks_table(env: dict) -> str:
    versions = package_versions(env)
    rows = []
    for _, label, orm, server, packages in STACK_ROWS:
        shown = ", ".join(
            f"{name} {versions[name]}" for name in packages if name in versions
        )
        rows.append(f"| {label} | {orm} | {server} | {shown} |")
    return (
        "| Stack | ORM and driver | Server | Versions |\n| --- | --- | --- | --- |\n"
        + "\n".join(rows)
    )


def main(directories: list[Path]) -> None:
    first_env = json.loads((directories[0] / "environment.json").read_text())
    parts = [
        "---\nstatus: implemented\n---\n",
        "# Benchmarks\n",
        "Four frameworks serve the same small API on one machine, against the same PostgreSQL data. "
        "This page shows what each of them did in these runs, and what the runs do not tell you.\n",
        '!!! warning "Read this first"\n'
        "    One machine, one day, a small warm dataset and a loopback connection. The numbers describe these "
        "implementations as configured here: they are not a ranking, a production-readiness claim or a statement "
        "about other hardware. Hinoki is pre-alpha. A scenario whose samples are too unstable to compare is "
        "listed as not reported instead of being compared.\n",
        "## What is measured\n",
        "One service, two tables, deterministic seed data (100 authors, 10 000 posts, 80 % published). "
        "Before any measurement, each stack must return the same responses as an oracle that reads PostgreSQL directly, "
        "and must issue the same number of SQL statements per request.\n",
        service_table() + "\n",
        stacks_table(first_env) + "\n",
        "## Results\n",
    ]
    for directory in directories:
        mode = json.loads((directory / "environment.json").read_text())["settings"].get(
            "server_mode", "documented"
        )
        title, intro = SLICE_TITLES[mode]
        parts.append(f'=== "{title}"\n')
        parts.append(indented(intro + "\n\n" + render_mode(directory)) + "\n")
    locations = ", ".join(
        sorted(
            {f"`benchmarks/comparative/results/{d.parent.name}/`" for d in directories}
        )
    )
    parts.extend(
        [
            "## How to read the numbers\n",
            "\n".join(f"- {note}" for note in READING_NOTES) + "\n",
            "## Reproduce\n",
            REPRODUCE + "\n",
            f"Raw `oha` JSON, environment records, server logs, per-worker CPU time and verification output are kept in {locations}. "
            "Setup details, the database container and the rules for repeating an unstable scenario are in `benchmarks/comparative/README.md`.",
        ]
    )
    print("\n".join(parts))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: report.py RESULTS_DIR [RESULTS_DIR ...]")
    main([Path(arg) for arg in sys.argv[1:]])
