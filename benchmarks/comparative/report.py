"""Generate the benchmark plots and tables from retained oha JSON."""

import html
import json
import statistics
import sys
from pathlib import Path
from typing import Any

JSONData = dict[str, Any]

FRAMEWORKS = {
    "hinoki": "Hinoki · Granian",
    "fastapi": "FastAPI · Uvicorn",
    "ninja": "Django Ninja · Uvicorn",
    "bolt": "Django Bolt · Actix",
    "hinoki-granian": "Hinoki · Granian",
    "hinoki-kiso": "Hinoki · Kiso",
    "fastapi-granian": "FastAPI · Granian",
    "ninja-granian": "Django Ninja · Granian",
}
SCENARIOS = ["list", "detail", "health", "create"]
EXPECTED = {"health": "200", "list": "200", "detail": "200", "create": "201"}


def load_runs(directory: Path, stack: str, scenario: str) -> list[JSONData]:
    files = sorted(directory.glob(f"{stack}-{scenario}-*.json"))
    if not files:
        raise ValueError(f"missing raw results for {stack} / {scenario} in {directory}")
    return [json.loads(path.read_text()) for path in files]


PERCENTILES = ("p50", "p95", "p99")


def spread(values: list[float], scale: float = 1.0) -> JSONData:
    return {
        "median": statistics.median(values) * scale,
        "low": min(values) * scale,
        "high": max(values) * scale,
    }


def run_summary(runs: list[JSONData], expected_status: str) -> JSONData:
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


def machine_lines(env: JSONData) -> list[str]:
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


def stack_names(directory: Path, settings: JSONData) -> list[str]:
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


def unstable(summaries: list[tuple[str, JSONData]]) -> bool:
    return any(row["cv_pct"] and row["cv_pct"] > STABLE_CV for _, row in summaries)


Rows = list[tuple[str, JSONData]]


def is_hinoki(label: str) -> bool:
    return label.startswith("Hinoki")


def rps(value: float) -> str:
    return f"{value:,.0f}"


def ratio(value: float, slowest: float) -> str:
    return f"{value / slowest:.2f}&times;" if slowest > 0 else "n/a"


def hinoki_position(rows: Rows) -> str:
    """Hinoki's own line when it is not the stack with the highest median."""
    slowest = rows[-1][1]["median"]
    for position, (label, row) in enumerate(rows[1:], start=2):
        if is_hinoki(label):
            return (
                f" <b>{html.escape(label)}</b>: {rps(row['median'])} requests/s, "
                f"{ratio(row['median'], slowest)} the slowest, position {position} of {len(rows)} by median."
            )
    return ""


def headline(rows: Rows) -> str:
    """One plain sentence per scenario, derived only from the sorted medians and run ranges."""
    label, top = rows[0]
    if len(rows) == 1:
        return (
            f"Median: <b>{html.escape(label)}</b> at {rps(top['median'])} requests/s."
        )
    slow_label, slow = rows[-1]
    text = (
        f"Highest median: <b>{html.escape(label)}</b> at {rps(top['median'])} requests/s, "
        f"{ratio(top['median'], slow['median'])} the slowest stack ({html.escape(slow_label)})."
    )
    if rows[1][1]["high"] >= top["low"]:
        text += " The run ranges of the top two stacks overlap, so their order is not settled."
    return text + hinoki_position(rows)


def sorted_rows(rows: Rows) -> Rows:
    return sorted(rows, key=lambda item: item[1]["median"], reverse=True)


def result_row(label: str, row: JSONData, lead: str = "") -> str:
    latency = row["latency_ms"]
    cv = "n/a" if row["cv_pct"] is None else f"{row['cv_pct']:.1f}%"
    return (
        f'<tr>{lead}<th scope="row">{html.escape(label)}</th>'
        f'<td>{row["median"]:,.0f}<span class="range">{row["low"]:,.0f}&ndash;{row["high"]:,.0f}</span></td>'
        f"<td>{fmt_ms(latency['p50']['median'])}</td><td>{fmt_ms(latency['p99']['median'])}</td>"
        f"<td>{cv}</td><td>{row['wrong']}</td><td>{row['runs']}</td></tr>"
    )


def reported_scenarios(
    directory: Path,
) -> tuple[dict[str, list[tuple[str, JSONData]]], list[str], list[str]]:
    env_path = directory / "environment.json"
    if not env_path.exists():
        raise ValueError(f"missing environment metadata in {directory}")
    settings = json.loads(env_path.read_text())["settings"]
    stacks = stack_names(directory, settings)
    excluded = single_process_stacks(directory, stacks, settings["workers"])
    stacks = [stack for stack in stacks if stack not in excluded]
    reported = {}
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
    return reported, withheld, excluded


def throughput_panel(
    scenario: str,
    request: str,
    description: str,
    rows: Rows,
    chart_id: str,
) -> str:
    rows = sorted_rows(rows)
    axis_max = rows[0][1]["median"] or 1.0
    slowest = rows[-1][1]["median"] or 1.0
    compared = len(rows) > 1
    chart_description = "; ".join(
        f"{label}: median {rps(row['median'])} requests per second, "
        f"range {rps(row['low'])} to {rps(row['high'])}"
        + (f", {row['median'] / slowest:.2f} times the slowest" if compared else "")
        for label, row in rows
    )
    tracks = "".join(
        bar_row(label, row, axis_max, slowest if compared else None)
        for label, row in rows
    )
    multiplier_note = (
        " The multiplier compares each stack with the slowest one in this chart."
        if compared
        else ""
    )
    return (
        f'<section class="bench-scenario" id="{chart_id}" aria-labelledby="{chart_id}-title">'
        '<header class="bench-scenario__head">'
        f'<p class="bench-scenario__kind" id="{chart_id}-kind">{html.escape(scenario.upper())}</p>'
        f'<h3 id="{chart_id}-title">{html.escape(request)}</h3>'
        f"<p>{html.escape(description)}</p></header>"
        f'<p class="bench-headline">{headline(rows)}</p>'
        f'<p id="{chart_id}-desc" class="bench-sr">{html.escape(chart_description)}.</p>'
        f'<div class="bench-chart" data-stacks="{len(rows)}" role="img" aria-labelledby="{chart_id}-kind {chart_id}-title" '
        f'aria-describedby="{chart_id}-desc">{tracks}</div>'
        '<p class="bench-scenario__note">Bars show median requests per second and start at zero; '
        f"longer means more requests served.{multiplier_note} {runs_note(rows)}</p>"
        '<details class="bench-details"><summary>Exact numbers: latency, lowest and highest run, CV</summary>'
        f"{results_table(scenario, rows)}</details></section>"
    )


def runs_note(rows: Rows) -> str:
    counts = sorted({row["runs"] for _, row in rows})
    if len(counts) == 1:
        return f"{counts[0]} measured runs per stack."
    return "Run counts differ per stack; see the table."


def bar_row(label: str, row: JSONData, axis_max: float, slowest: float | None) -> str:
    name, _, server = label.partition(" · ")
    server_html = f"<small>{html.escape(server)}</small>" if server else ""
    row_class = "bench-row is-hinoki" if is_hinoki(label) else "bench-row"
    ratio_html = (
        f'<span class="bench-ratio">{ratio(row["median"], slowest)}</span>'
        if slowest
        else ""
    )
    return (
        f'<div class="{row_class}">'
        f'<span class="bench-name"><b>{html.escape(name)}</b>{server_html}</span>'
        '<span class="bench-track" aria-hidden="true">'
        f'<span class="bench-bar" style="width:{row["median"] / axis_max * 100:.2f}%"></span>'
        "</span>"
        f'<span class="bench-value">{rps(row["median"])}<small> req/s</small></span>'
        f"{ratio_html}</div>"
    )


def summary_card(scenario: str, rows: Rows, chart_id: str) -> str:
    rows = sorted_rows(rows)
    label, top = rows[0]
    if len(rows) == 1:
        note = f"Median of {top['runs']} runs"
    elif is_hinoki(label):
        note = f"{ratio(top['median'], rows[-1][1]['median'])} the slowest"
    else:
        note = hinoki_summary_line(rows)
    return (
        f'<a class="bench-card" href="#{chart_id}-{scenario}">'
        f'<span class="bench-card__kind">{html.escape(scenario.upper())}</span>'
        f'<span class="bench-card__caption">{"Median" if len(rows) == 1 else "Highest median"}</span>'
        f'<span class="bench-card__lead">{html.escape(label)}</span>'
        f'<span class="bench-card__value">{rps(top["median"])}<small> req/s</small></span>'
        f'<span class="bench-card__note">{note}</span></a>'
    )


def hinoki_summary_line(rows: Rows) -> str:
    slowest = rows[-1][1]["median"]
    for label, row in rows:
        if is_hinoki(label):
            return (
                f"{html.escape(label.partition(' · ')[0])}: {rps(row['median'])} req/s, "
                f"{ratio(row['median'], slowest)} the slowest"
            )
    return f"{len(rows)} stacks compared"


def summary_cards(reported: dict[str, Rows], withheld: list[str], chart_id: str) -> str:
    cards = [
        summary_card(scenario, reported[scenario], chart_id)
        for scenario, _, _ in SERVICE_ROWS
        if reported.get(scenario)
    ]
    cards.extend(
        f'<a class="bench-card is-withheld" href="#{chart_id}-withheld">'
        f'<span class="bench-card__kind">{html.escape(scenario.upper())}</span>'
        '<span class="bench-card__caption">Withheld</span>'
        '<span class="bench-card__lead">Not compared</span>'
        f'<span class="bench-card__note">Requests-per-second CV above {STABLE_CV}%</span></a>'
        for scenario in withheld
    )
    if not cards:
        return ""
    return f'<nav class="bench-cards" aria-label="Summary of each workload">{"".join(cards)}</nav>'


def throughput_chart(
    reported: dict[str, Rows],
    chart_id: str,
) -> str:
    rows = []
    for scenario, request, description in SERVICE_ROWS:
        if reported.get(scenario):
            rows.append(
                throughput_panel(
                    scenario,
                    request,
                    description,
                    reported[scenario],
                    f"{chart_id}-{scenario}",
                )
            )
    return f'<div class="bench-results">{"".join(rows)}</div>'


def results_table(scenario: str, rows: list[tuple[str, JSONData]]) -> str:
    rows_html = "".join(result_row(label, row) for label, row in rows)
    return (
        '<div class="benchmark-table-wrap"><table class="benchmark-table">'
        f"<caption>{scenario.title()} results: medians and per-run range for requests per second</caption>"
        '<thead><tr><th scope="col">Stack</th><th scope="col">Requests/s</th>'
        '<th scope="col">p50 ms</th><th scope="col">p99 ms</th><th scope="col">CV</th>'
        '<th scope="col">Unexpected</th><th scope="col">Runs</th></tr></thead>'
        f"<tbody>{rows_html}</tbody></table></div>"
    )


SERVICE_ROWS = (
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
        "health",
        "GET /health",
        "Static JSON, no database: a diagnostic of the framework and server path.",
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
        "compare each stack on its documented server",
    ),
    "same-granian": (
        "Same server for the ASGI stacks",
        "Hinoki, FastAPI and Django Ninja run on one Granian version and configuration, which takes the server out of the comparison. "
        "Django Bolt is not in this slice because it does not expose ASGI.",
        "compare the three ASGI stacks on Granian",
    ),
    "kiso": (
        "Hinoki on its Rust server",
        "Only Hinoki, served by Kiso (`hinoki serve --server kiso`, 4 workers) instead of Granian. Kiso is not an ASGI server, so the other stacks have no counterpart here. "
        "This was a separate session from the other two views, so read it next to them with that in mind; it is not a ranking.",
        "see Hinoki on its own Rust server (Kiso)",
    ),
}
SETUP_COUNTS = {1: "One server setup", 2: "Two server setups", 3: "Three server setups"}
HOW_TO_READ = (
    '!!! tip "How to read this"\n'
    "    Each card and chart is one request type. Longer bars mean more requests per second on that workload; "
    "the number is the median of the measured runs. The multiplier (for example 2.00&times;) compares a stack with the slowest stack "
    "in the same chart, and Hinoki rows are drawn in the brand colour and bold type. "
    'Open "Exact numbers" under a chart for latency, the lowest and highest run and the run-to-run variation (CV).\n'
)
READING_NOTES = (
    "**Requests per second and latency are one measurement.** Every run keeps 64 connections busy, so higher latency means fewer requests per second; the two do not confirm each other.",
    "**Each chart starts at 0 and shows the median requests per second.** Bars are sorted from highest to lowest within a workload and server setup. Each panel has its own scale; use the tables for exact values and the lowest-to-highest run range.",
    "**The multiplier is one median divided by the slowest median in the same chart.** It is a ratio of medians and carries their run-to-run spread; it does not compare charts with each other.",
    "**p50 and p99 are medians of per-run percentiles**, not percentiles of all requests pooled together. A range is descriptive, not a confidence interval.",
    f"**CV is the coefficient of variation of requests per second across runs.** Above {STABLE_CV}% for any stack, the scenario is not reported: the samples are too unstable to compare, and the raw files are kept.",
    "**What this does not cover:** TLS, proxies, authentication, compression, memory, cold data, larger tables or other hardware. Pool sizes are equal per process, but each library schedules work differently.",
)


def reproduce(modes: list[str]) -> str:
    runs = "\n".join(
        f"uv run --locked python run.py --server-mode {mode} --out results/YYYY-MM-DD/{mode}"
        for mode in modes
    )
    outputs = " ".join(f"results/YYYY-MM-DD/{mode}" for mode in modes)
    return (
        "```sh\ncd benchmarks/comparative\nuv sync --locked\n"
        "VIRTUAL_ENV=$PWD/.venv uvx maturin develop --release --uv --manifest-path ../../crates/bindings/Cargo.toml\n"
        f"{runs}\nuv run --locked python report.py {outputs} > ../../docs/benchmarks.md\n```"
    )


def package_versions(env: JSONData) -> dict[str, str]:
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
    reported, withheld, excluded = reported_scenarios(directory)
    lines: list[str] = []
    for stack in excluded:
        lines.extend(
            [
                f"{FRAMEWORKS[stack]} is not reported here: it was started with {settings['workers']} processes, "
                f"but its per-process CPU time (`{stack}-workers.txt`) shows one process did nearly all the work, "
                "so it is not a comparable result. Its raw samples are retained.",
                "",
            ]
        )
    chart_id = f"bench-{slug(directory)}"
    lines.extend(
        [
            summary_cards(reported, withheld, chart_id),
            "",
            throughput_chart(reported, chart_id),
            "",
        ]
    )
    if withheld:
        lines.extend(
            [
                f'<aside class="bench-withheld" id="{chart_id}-withheld">'
                '<p class="bench-scenario__kind">WITHHELD</p>'
                f"<h3>{', '.join(name.title() for name in withheld)} — not compared</h3>"
                f"<p>Not compared: requests-per-second CV exceeded {STABLE_CV}% for at least one stack. "
                "The samples remain in the retained results.</p>"
                f"<code>benchmarks/comparative/results/{html.escape(directory.parent.name)}/</code>"
                "</aside>",
                "",
            ]
        )
    lines.extend(
        [
            "All listed stacks passed the response and SQL statement-count verifier before measurement. "
            "Requests in flight at the end of a sample were aborted by `oha`; they are not counted as unexpected responses.",
            "",
            '??? info "Run environment"',
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


def stacks_table(env: JSONData, modes: list[str]) -> str:
    versions = package_versions(env)
    rows = []
    for key, label, orm, server, packages in STACK_ROWS:
        shown = ", ".join(
            f"{name} {versions[name]}" for name in packages if name in versions
        )
        if key == "hinoki" and "kiso" in modes:
            shown_server = f"{server}; Kiso in its own view"
        else:
            shown_server = server
        rows.append(f"| {label} | {orm} | {shown_server} | {shown} |")
    return (
        "| Stack | ORM and driver | Server | Versions |\n| --- | --- | --- | --- |\n"
        + "\n".join(rows)
    )


def server_mode(directory: Path) -> str:
    settings = json.loads((directory / "environment.json").read_text())["settings"]
    return str(settings.get("server_mode", "documented"))


def results_intro(modes: list[str]) -> str:
    phrases = [SLICE_TITLES[mode][2] for mode in modes]
    if len(phrases) == 1:
        switch = ""
    elif len(phrases) == 2:
        switch = f"Switch the server setup to {phrases[0]} or {phrases[1]}. "
    else:
        switch = (
            f"Switch the server setup to {', '.join(phrases[:-1])}, or {phrases[-1]}. "
        )
    return (
        switch
        + "Each chart starts at zero and uses its own scale; exact values and run ranges are under each chart.\n"
    )


def collapsed(title: str, body: str) -> str:
    return f'??? info "{title}"\n\n{indented(body)}\n'


def main(directories: list[Path]) -> None:
    first_env = json.loads((directories[0] / "environment.json").read_text())
    measured_at = html.escape(first_env["date"])
    machine = html.escape(first_env["machine"])
    cpu = html.escape(first_env["cpu"])
    cores = first_env["cores"]
    concurrency = first_env["settings"]["concurrency"]
    run_count = first_env["settings"]["runs"]
    modes = [server_mode(directory) for directory in directories]
    parts = [
        f'<p class="bench-eyebrow">FIELD NOTE · {measured_at} · {machine}</p>\n',
        "# Benchmarks\n",
        f"## One API. {SETUP_COUNTS.get(len(modes), f'{len(modes)} server setups')}.\n",
        "Four framework stacks served the same small API and PostgreSQL data. These measurements describe "
        "one machine and one benchmark session; they do not rank frameworks or predict production performance.\n",
        f'<div class="bench-run-strip"><span><b>01</b>{cpu}</span><span><b>02</b>{cores} logical CPUs</span>'
        f"<span><b>03</b>{concurrency} concurrent requests</span><span><b>04</b>{run_count} measured runs</span></div>\n",
        '!!! warning "Read this first"\n'
        "    Hinoki is pre-alpha. The run used one host, warm data and loopback networking. It is not a framework ranking or a production claim. Unstable samples stay visible as withheld results.\n",
        "## Results\n",
        HOW_TO_READ,
        results_intro(modes),
    ]
    for directory, mode in zip(directories, modes, strict=True):
        title, intro, _ = SLICE_TITLES[mode]
        parts.append(f'=== "{title}"\n')
        parts.append(indented(intro + "\n\n" + render_mode(directory)) + "\n")
    locations = ", ".join(
        sorted(
            {f"`benchmarks/comparative/results/{d.parent.name}/`" for d in directories}
        )
    )
    parts.extend(
        [
            "## What was measured\n",
            "One service, two tables and deterministic seed data: 100 authors and 10,000 posts, 80% published. Every stack had to match a PostgreSQL oracle and the same SQL statement count per request before measurement. "
            "The stacks differ in ORM, database driver and server, as listed below.\n",
            collapsed(
                "Workloads, stacks and versions",
                service_table() + "\n\n" + stacks_table(first_env, modes),
            ),
            "## How to read the numbers\n",
            collapsed(
                "Measurement notes and limits",
                "\n".join(f"- {note}" for note in READING_NOTES),
            ),
            "## Reproduce\n",
            reproduce(modes) + "\n",
            f"Raw `oha` JSON, environment records, server logs, per-worker CPU time and verification output are kept in {locations}. "
            "Setup details, the database container and the rules for repeating an unstable scenario are in `benchmarks/comparative/README.md`.",
        ]
    )
    print("\n".join(parts))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: report.py RESULTS_DIR [RESULTS_DIR ...]")
    main([Path(arg) for arg in sys.argv[1:]])
