"""Generate the benchmark page from retained raw JSON.

Every number on the page is computed here from the raw samples of the results
directories given on the command line; none is written by hand. The first
directories (any mode except ``same-granian``) feed the overview, where every
stack sits on one scale; ``documented`` and ``same-granian`` directories are
also shown as expert views.

The request types are Django Bolt's seven routes. Results are ``bombardier``
documents (``{stack}-{scenario}-r{start}-{n}.json``); ``oha`` documents
(``{stack}-{scenario}-{n}.json``, one start) are still read, but the two formats
measured differently and the report says so instead of mixing them.
"""

import html
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any, NamedTuple

import loadtool
import parity_data
import report_html
from archive_refs import comparative_results_reference, result_date
from report_html import Cell, Scenario, rps

JSONData = dict[str, Any]


PYTHON = "Python"
OTHER_RUNTIMES = "Other runtimes"


class StackInfo(NamedTuple):
    family: str
    framework: str
    server: str
    group: str = PYTHON


STACKS = {
    "hinoki": StackInfo("hinoki", "Hinoki", "Granian"),
    "hinoki-granian": StackInfo("hinoki", "Hinoki", "Granian"),
    "hinoki-kiso": StackInfo("hinoki-kiso", "Hinoki", "Kiso, native server"),
    "litestar": StackInfo("litestar", "Litestar", "Uvicorn"),
    "litestar-granian": StackInfo("litestar", "Litestar", "Granian"),
    "fastapi": StackInfo("fastapi", "FastAPI", "Uvicorn"),
    "fastapi-granian": StackInfo("fastapi", "FastAPI", "Granian"),
    "ninja": StackInfo("ninja", "Django Ninja", "Uvicorn"),
    "ninja-granian": StackInfo("ninja", "Django Ninja", "Granian"),
    "bolt": StackInfo("bolt", "Django Bolt", "Actix, native server"),
    "elysia-bun": StackInfo("elysia-bun", "Elysia", "Bun", OTHER_RUNTIMES),
    "hono-bun": StackInfo("hono-bun", "Hono", "Bun", OTHER_RUNTIMES),
    "hono-node": StackInfo("hono-node", "Hono", "Node.js", OTHER_RUNTIMES),
}
STACK_ORDER = list(STACKS)
GRANIAN_KEYS = ("hinoki", "hinoki-granian")
KISO_KEY = "hinoki-kiso"
# Django Bolt's seven routes in its order: no database first, then the database
# and finally the routes behind the JWT cookie.
SCENARIOS = [
    "json-1k",
    "json-10k",
    "db",
    "articles",
    "article",
    "auth-me",
    "auth-articles",
]
SCENARIO_ORDER = SCENARIOS
EXPECTED = dict.fromkeys(SCENARIOS, "200")
START = re.compile(r"-r(\d+)-\d+\.json$")
STABLE_CV = 15
TIE_PCT = 10
TIMES = "\u00d7"
NUMBER_WORDS = {2: "two", 3: "three", 4: "four"}


def stack_label(stack: str) -> str:
    info = STACKS[stack]
    return f"{info.framework} ({info.server})"


def stack_family(stack: str) -> str:
    return STACKS[stack].family


def stack_group(stack: str) -> str:
    return STACKS[stack].group


def stack_rank(stack: str) -> int:
    return STACK_ORDER.index(stack)


def load_runs(directory: Path, stack: str, scenario: str) -> list[JSONData]:
    files = sorted(directory.glob(f"{stack}-{scenario}-*.json"))
    if not files:
        raise ValueError(f"missing raw results for {stack} / {scenario} in {directory}")
    runs = []
    for path in files:
        found = START.search(path.name)
        runs.append(
            {**json.loads(path.read_text()), "_start": int(found[1]) if found else 1}
        )
    return runs


PERCENTILES = ("p50", "p95", "p99")


def spread(values: list[float], scale: float = 1.0) -> JSONData:
    return {
        "median": statistics.median(values) * scale,
        "low": min(values) * scale,
        "high": max(values) * scale,
    }


def normalize(run: JSONData, expected_status: str) -> JSONData:
    """Rate, latency percentiles in ms and unexpected count of one raw sample."""
    if "result" in run:
        summary = loadtool.summarize(run)
        return {**summary, "start": run.get("_start", 1)}
    wrong = sum(
        count
        for code, count in run["statusCodeDistribution"].items()
        if code != expected_status
    ) + sum(
        count
        for kind, count in run["errorDistribution"].items()
        if kind != "aborted due to deadline"
    )
    return {
        "rps": float(run["summary"]["requestsPerSec"]),
        "latency_ms": {
            name: run["latencyPercentiles"][name] * 1000 for name in PERCENTILES
        },
        "wrong": wrong,
        "start": run.get("_start", 1),
    }


def run_summary(runs: list[JSONData], expected_status: str) -> JSONData:
    """Aggregate every sample of every start: medians plus min-max, none discarded.

    The median, range and CV pool all samples; ``start_medians`` shows each
    server start's own median so a start-to-start shift stays visible.
    """
    samples = [normalize(run, expected_status) for run in runs]
    rates = [sample["rps"] for sample in samples]
    mean_rps = statistics.mean(rates)
    starts = sorted({sample["start"] for sample in samples})
    return {
        "runs": len(samples),
        "starts": len(starts),
        "median": statistics.median(rates),
        "low": min(rates),
        "high": max(rates),
        "samples": rates,
        "cv_pct": statistics.stdev(rates) / mean_rps * 100 if len(rates) > 1 else None,
        "start_medians": [
            statistics.median(s["rps"] for s in samples if s["start"] == start)
            for start in starts
        ],
        "latency_ms": {
            name: spread([sample["latency_ms"][name] for sample in samples])
            for name in PERCENTILES
        },
        "wrong": sum(sample["wrong"] for sample in samples),
    }


def is_tie(higher: float, lower: float) -> bool:
    """A gap under TIE_PCT of the higher median is a tie, not a ranking."""
    return higher > 0 and (higher - lower) / higher * 100 < TIE_PCT


def instability(row: JSONData) -> str | None:
    """Why a result is unstable (CV above STABLE_CV), or None when it is stable."""
    cv = row["cv_pct"]
    if not cv or cv <= STABLE_CV:
        return None
    text = (
        f"The coefficient of variation (CV) of its requests per second is {cv:.1f}%, above {STABLE_CV}%: "
        f"the samples range from {rps(row['low'])} to {rps(row['high'])}."
    )
    starts = row["start_medians"]
    if len(starts) > 1 and not is_tie(max(starts), min(starts)):
        word = NUMBER_WORDS.get(len(starts), str(len(starts)))
        shown = " and ".join(rps(value) for value in starts)
        text += f" The {word} server starts gave medians of {shown}."
    return text + " The median is shown but not used for the highest or tied marks."


def compare(first: JSONData, second: JSONData) -> str:
    """How `first` stands against `second`: higher, lower, tie or unsettled.

    A gap under TIE_PCT is a tie; a larger gap counts only when the sample
    ranges do not overlap and neither result is unstable.
    """
    if instability(first) or instability(second):
        return "unsettled"
    top, bottom = (
        (first, second) if first["median"] >= second["median"] else (second, first)
    )
    if is_tie(top["median"], bottom["median"]):
        return "tie"
    if top["low"] <= bottom["high"]:
        return "unsettled"
    return "higher" if top is first else "lower"


def best_stacks(rows: dict[str, JSONData]) -> tuple[str | None, set[str]]:
    """The stable stack with the highest median and the stable stacks tied with it."""
    stable = {key: row for key, row in rows.items() if not instability(row)}
    if not stable:
        return None, set()
    best = max(stable, key=lambda key: stable[key]["median"])
    top = stable[best]["median"]
    tied = {
        key for key, row in stable.items() if key != best and is_tie(top, row["median"])
    }
    return best, tied


def machine_text(env: JSONData) -> str:
    return f"{env['cpu']}, {env['cores']} logical CPUs, {env['machine']}"


def tool_name(env: JSONData) -> str:
    return str(env.get("load_tool", "oha"))


def tool_label(env: JSONData) -> str:
    if tool_name(env) == "oha":  # the recorded value is `oha --version` output
        return str(env.get("oha", "oha (version not recorded)"))
    return f"{tool_name(env)} {env.get(tool_name(env), 'version not recorded')}"


def sampling_text(settings: JSONData) -> str:
    runs = settings["runs"]
    starts = settings.get("restarts", 1)
    each = (
        f"{settings['requests']:,} requests"
        if "requests" in settings
        else f"{settings['duration_s']} s"
    )
    if "restarts" in settings:
        return (
            f"{starts} server starts, each with {runs} sample(s) of {each} per scenario"
        )
    return f"{runs} measured runs of {each}"


def stack_names(directory: Path, settings: JSONData) -> list[str]:
    stacks = settings.get("stacks") or [
        name for name in STACKS if any(directory.glob(f"{name}-{SCENARIOS[0]}-*.json"))
    ]
    if not stacks:
        raise ValueError(f"no benchmark stacks listed or found in {directory}")
    for stack in stacks:
        if stack not in STACKS:
            raise ValueError(f"unknown stack {stack!r} in {directory}")
        if not (directory / f"{stack}-verify.txt").exists():
            raise ValueError(f"missing verification output for {stack} in {directory}")
    return stacks


def cpu_seconds(text: str) -> float:
    """Parse `ps` CPU time: `mm:ss.ss` or `[dd-]hh:mm:ss`."""
    days, _, clock = text.rpartition("-")
    *larger, seconds = clock.split(":")
    total = float(seconds)
    for unit, part in zip((60, 3600), reversed(larger), strict=False):
        total += int(part) * unit
    return total + (int(days) * 86400 if days else 0)


def file_share(path: Path, workers: int) -> float | None:
    times = sorted(
        (cpu_seconds(line.split()[2]) for line in path.read_text().splitlines()[1:]),
        reverse=True,
    )[:workers]
    total = sum(times)
    return times[0] / total if workers > 1 and len(times) > 1 and total else None


def busy_share(directory: Path, stack: str, workers: int) -> float | None:
    """Largest share of CPU time held by one of the `workers` busiest processes of any start."""
    shares = [
        share
        for path in sorted(directory.glob(f"{stack}-workers*.txt"))
        if (share := file_share(path, workers)) is not None
    ]
    return max(shares, default=None)


def single_process_stacks(
    directory: Path, stacks: list[str], workers: int
) -> list[str]:
    """Stacks where one process did nearly all the work despite several being started."""
    shares = {stack: busy_share(directory, stack, workers) for stack in stacks}
    return [stack for stack, share in shares.items() if share and share > 0.8]


def server_mode(directory: Path) -> str:
    settings = json.loads((directory / "environment.json").read_text())["settings"]
    return str(settings.get("server_mode", "documented"))


def read_environment(directory: Path) -> JSONData:
    env_path = directory / "environment.json"
    if not env_path.exists():
        raise ValueError(f"missing environment metadata in {directory}")
    env: JSONData = json.loads(env_path.read_text())
    return env


def measured_scenarios(settings: JSONData) -> list[str]:
    """The routes a run measured (``--scenario`` may narrow them), in Bolt's order."""
    chosen = set(settings.get("scenarios") or SCENARIOS)
    return [scenario for scenario in SCENARIOS if scenario in chosen]


def scenario_rows(
    directory: Path,
) -> tuple[dict[str, dict[str, JSONData]], list[str]]:
    """Summaries per scenario and stack, and the stacks left out for worker imbalance."""
    settings = read_environment(directory)["settings"]
    stacks = stack_names(directory, settings)
    excluded = single_process_stacks(directory, stacks, settings["workers"])
    stacks = [stack for stack in stacks if stack not in excluded]
    rows = {
        scenario: {
            stack: run_summary(
                load_runs(directory, stack, scenario), EXPECTED[scenario]
            )
            for stack in stacks
        }
        for scenario in measured_scenarios(settings)
    }
    return rows, excluded


Rows = dict[str, dict[str, JSONData]]


def collect(directories: list[Path]) -> tuple[Rows, list[str]]:
    """Merge the directories into one stack table per scenario; the first stack seen wins."""
    merged: Rows = {}
    notes: list[str] = []
    for directory in directories:
        rows, excluded = scenario_rows(directory)
        workers = read_environment(directory)["settings"]["workers"]
        notes.extend(exclusion_note(stack, workers) for stack in excluded)
        for scenario, stacks in rows.items():
            slot = merged.setdefault(scenario, {})
            for stack, summary in stacks.items():
                slot.setdefault(stack, summary)
    return merged, notes


def exclusion_note(stack: str, workers: int) -> str:
    return (
        f"{stack_label(stack)} is not reported: it was started with {workers} processes, "
        "but its per-process CPU time shows one process did nearly all the work, "
        "so it is not a comparable result. Its raw samples are retained."
    )


def ratio_text(ratio: float) -> str:
    return f"{ratio:.1f}{TIMES}" if ratio < 10 else f"{ratio:.0f}{TIMES}"


def standing(first: JSONData, second: JSONData) -> tuple[str, str]:
    """How `first` stands against `second`: the verdict and a short phrase.

    The ratio is of medians and always at least 1, worded in the direction it
    points ("2.4x faster", "1.4x slower").
    """
    verdict = compare(first, second)
    gap = first["median"] / second["median"] if second["median"] else 0.0
    return verdict, {
        "higher": f"{ratio_text(gap)} faster",
        "lower": f"{ratio_text(1 / gap if gap else 0.0)} slower",
        "tie": f"tie (within {TIE_PCT}%)",
        "unsettled": "not settled",
    }[verdict]


def reference_key(rows: Rows) -> str:
    """The stack every other is compared with: Kiso, else Hinoki on Granian, else the first."""
    present = sorted(
        set().union(*(set(stacks) for stacks in rows.values())), key=stack_rank
    )
    for key in (KISO_KEY, *GRANIAN_KEYS):
        if key in present:
            return key
    return present[0]


def make_cells(rows: dict[str, JSONData], ref: str) -> tuple[Cell, ...]:
    best, tied = best_stacks(rows)
    cells = []
    for key in sorted(rows, key=lambda stack: (stack != ref, stack_rank(stack))):
        row = rows[key]
        verdict, versus = (
            ("reference", "reference")
            if key == ref
            else standing(row, rows[ref])
            if ref in rows
            else ("", "")
        )
        cells.append(
            Cell(
                key=key,
                family=stack_family(key),
                label=stack_label(key),
                median=row["median"],
                low=row["low"],
                high=row["high"],
                p50_ms=row["latency_ms"]["p50"]["median"],
                p99_ms=row["latency_ms"]["p99"]["median"],
                cv_pct=row["cv_pct"],
                samples=tuple(row["samples"]),
                wrong=row["wrong"],
                mark="best" if key == best else "tie" if key in tied else "",
                reason=instability(row),
                versus=versus,
                verdict=verdict,
            )
        )
    return tuple(cells)


def versus_keys(rows: Rows) -> tuple[str, str] | None:
    """The Hinoki-on-Kiso and Hinoki-on-Granian stacks, when the rows hold both."""
    present = set().union(*(set(stacks) for stacks in rows.values())) if rows else set()
    granian = next((key for key in GRANIAN_KEYS if key in present), None)
    return (KISO_KEY, granian) if granian and KISO_KEY in present else None


def make_scenarios(rows: Rows) -> list[Scenario]:
    meta = {name: (request, what) for name, request, what in SERVICE_ROWS}
    ref = reference_key(rows)
    return [
        Scenario(
            key=key,
            name=scenario_name(key),
            request=meta[key][0],
            description=meta[key][1],
            cells=make_cells(rows[key], ref),
            reference=stack_label(ref),
        )
        for key in sorted(rows, key=SCENARIO_ORDER.index)
    ]


def lead_finding(rows: Rows) -> str:
    pair = versus_keys(rows)
    if not pair:
        return ""
    kiso, granian = pair
    verdicts = Counter(
        compare(stacks[kiso], stacks[granian])
        for stacks in rows.values()
        if {kiso, granian} <= set(stacks)
    )
    text = (
        f"{stack_label(kiso)} against {stack_label(granian)}: ahead in {verdicts['higher']} "
        f"of {sum(verdicts.values())} request types, tied (within {TIE_PCT}%) in {verdicts['tie']}, "
        f"behind in {verdicts['lower']}"
    )
    if verdicts["unsettled"]:
        text += f", not settled in {verdicts['unsettled']} (overlapping ranges or an unstable result)"
    return text + "."


def total_samples(settings: JSONData) -> int:
    return int(settings["runs"]) * int(settings.get("restarts", 1))


def scenario_name(scenario: str) -> str:
    return SCENARIO_NAMES[scenario]


SCENARIO_NAMES = {
    "json-1k": "JSON 1 KB",
    "json-10k": "JSON 10 KB",
    "db": "Users",
    "articles": "Articles",
    "article": "Article",
    "auth-me": "Auth: me",
    "auth-articles": "Auth: articles",
}
SERVICE_ROWS = (
    (
        "json-1k",
        "GET /json-1k",
        f"A fixed JSON body of about 1 KB ({len(parity_data.BODIES['json-1k']):,} bytes), no database.",
    ),
    (
        "json-10k",
        "GET /json-10k",
        f"A fixed JSON body of about 10 KB ({len(parity_data.BODIES['json-10k']):,} bytes), no database.",
    ),
    (
        "db",
        "GET /db",
        "Ten users read from PostgreSQL: one SELECT.",
    ),
    (
        "articles",
        "GET /articles?page=1&page_size=20",
        "A page of articles with their author and tags and the total: a COUNT, the page joined with its author, then the tags of the page.",
    ),
    (
        "article",
        "GET /articles/1",
        "One article with its author, tags and comments: the article joined with its author, its comments, then its tags.",
    ),
    (
        "auth-me",
        "GET /auth/me",
        "Validate the HS256 JWT in the access_token cookie and return its user: one SELECT.",
    ),
    (
        "auth-articles",
        "GET /auth/articles?page=1&page_size=20",
        "The articles page behind the same JWT check: the user's SELECT, then the articles page.",
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
        "bolt",
        "Django Bolt",
        "Django async ORM, psycopg 3 pool",
        "its own Actix server",
        ("django-bolt",),
    ),
    (
        "litestar",
        "Litestar",
        "SQLAlchemy async, asyncpg, pool_pre_ping",
        "Uvicorn with uvloop and httptools",
        ("litestar", "sqlalchemy", "asyncpg", "uvicorn"),
    ),
    (
        "fastapi",
        "FastAPI",
        "SQLAlchemy async, asyncpg, pool_pre_ping",
        "Uvicorn with uvloop and httptools",
        ("fastapi", "sqlalchemy", "asyncpg", "uvicorn"),
    ),
    (
        "ninja",
        "Django Ninja",
        "Django ORM on a bounded thread pool, psycopg 3 pool, django-ninja-jwt",
        "Uvicorn with uvloop and httptools",
        ("django-ninja", "django", "psycopg", "django-ninja-jwt"),
    ),
    (
        "elysia-bun",
        "Elysia on Bun",
        "Bun.SQL (built in), prepared statements",
        "Bun.serve, processes sharing the port (SO_REUSEPORT) above one process",
        ("elysia", "Bun"),
    ),
    (
        "hono-bun",
        "Hono on Bun",
        "Bun.SQL (built in), prepared statements",
        "Bun.serve, processes sharing the port (SO_REUSEPORT) above one process",
        ("hono", "Bun"),
    ),
    (
        "hono-node",
        "Hono on Node.js",
        "node-postgres (pg), named prepared statements",
        "@hono/node-server on Node.js, node:cluster above one process",
        ("hono", "@hono/node-server", "pg", "Node.js"),
    ),
)
RUNTIME_NAMES = {"node": "Node.js", "bun": "Bun"}
OTHER_RUNTIMES_NOTE = (
    "Elysia on Bun, Hono on Bun and Hono on Node.js are the JavaScript servers of Django Bolt's comparison; "
    "they run on JavaScript runtimes instead of Python. "
    "They serve the same routes, pass the same verifier and use the same number of processes, "
    "connections per process and CPU placement; their language, runtime and database driver differ."
)
READING_NOTES = (
    "**Requests per second and latency are one measurement.** Every sample keeps the stated number of connections busy, so higher latency means fewer requests per second; the two do not confirm each other.",
    "**Bars start at 0 and show the median requests per second.** Within a request table every stack shares one scale, and the reference stack comes first, then the others in a fixed order, so the page is not sorted into a ranking. The lowest to highest sample is in the last column.",
    "**The comparison column is the ratio of two medians**, always at least 1 and worded in the direction it points (`2.4\u00d7 faster`, `1.4\u00d7 slower`). It is computed from the same raw samples as the medians.",
    "**8 workers and 1 worker are separate runs.** Each stack is started with that many server processes (and the same pool per process); the two groups are not added together and a 1-worker number says nothing about scaling.",
    "**Samples are pooled, none is discarded.** A stack is started several times; the median, the range and the CV are computed over every sample of every start, and an unstable result lists them all. "
    "The exact tables also give the upper median, `sorted(samples)[len // 2]`, the statistic Django Bolt's comparison publishes; with an even number of samples it is the higher middle sample.",
    "**p50 and p99 are medians of per-sample percentiles**, not percentiles of all requests pooled together. A range is descriptive, not a confidence interval.",
    f"**A gap under {TIE_PCT}% between two medians is a tie.** Differences that small are inside what this setup resolves, so no order is claimed. A larger gap is called a difference only when the sample ranges do not overlap.",
    f"**CV is the coefficient of variation of requests per second across samples.** Above {STABLE_CV}% the result is labelled Unstable: its median and samples stay on the page, but it is not given the highest or tied mark and no comparison with it is claimed.",
    "**What this does not cover:** TLS, proxies, compression, writes, cold data, larger tables, container memory limits or other hardware. Pool sizes are equal per process, but each library schedules work differently. It is not a ranking and not a production claim.",
)
LEGACY_NOTE = (
    "A view was sampled with `oha` for a fixed duration with 64 connections and one server start, "
    "so its format and method differ from the `bombardier` fixed-request runs (100 connections, several starts). "
    "The two formats are not comparable and must not be mixed in one comparison."
)


def out_dir(mode: str, workers: int, multi: bool) -> str:
    """Where a run's raw results go: one folder per mode, `wN/` for extra worker counts."""
    return (
        f"results/YYYY-MM-DD/{f'w{workers}/' if multi and workers != 1 else ''}{mode}"
    )


def run_command(directory: Path, settings: JSONData, multi: bool = False) -> str:
    mode = server_mode(directory)
    base = f"uv run --locked python run.py --server-mode {mode}"
    if "restarts" not in settings:
        return f"{base} --out {out_dir(mode, 1, False)} (script of the `oha` era, see git history)"
    stop = (
        f"--requests {settings['requests']}"
        if "requests" in settings
        else f"--duration {settings['duration_s']}"
    )
    sizing = "".join(
        f" --{flag} {settings[key]}"
        for flag, key in (("workers", "workers"), ("pool", "pool_per_worker"))
        if key in settings
    )
    return (
        f"{base}{sizing} --restarts {settings['restarts']} --runs {settings['runs']} "
        f"{stop} --out {out_dir(mode, settings.get('workers', 1), multi)}"
    )


def warmup_text(settings: JSONData) -> str:
    warmup = settings.get("warmup")
    if isinstance(warmup, dict):
        return (
            f"{warmup['requests']:,} requests at {warmup['connections']} connections "
            "per scenario and start"
        )
    return f"{settings['warmup_s']} s per scenario and start"


def memory_text(env: JSONData) -> str:
    size = env.get("host", {}).get("memory_bytes")
    return f"{int(size) // 2**30} GiB" if size else "not recorded"


def placement_text(settings: JSONData) -> str:
    if settings.get("server_cpus"):
        return f"server processes on CPUs {settings['server_cpus']}; load generator on CPUs {settings['load_cpus']}"
    return "no CPU pinning (the operating system scheduled the servers and the load generator)"


def unique_text(values: list[str]) -> str:
    return "; ".join(dict.fromkeys(values))


def command_label(directory: Path, env: JSONData, multi: bool) -> str:
    mode = server_mode(directory)
    return (
        f"Command ({mode}, {worker_name(env['settings']['workers'])})"
        if multi
        else f"Command ({mode})"
    )


def conditions_table(directories: list[Path]) -> str:
    envs = [read_environment(d) for d in directories]
    first = envs[0]
    settings = first["settings"]
    multi = len({e["settings"]["workers"] for e in envs}) > 1
    rows = [
        ("Date", ", ".join(str(env["date"]) for env in envs)),
        ("CPU", f"{first['cpu']} ({first['cores']} logical CPUs)"),
        ("Memory", memory_text(first)),
        ("Operating system", str(first["machine"])),
        ("Load generator", tool_label(first)),
        ("Connections", str(settings["concurrency"])),
        (
            "Requests per sample",
            f"{settings['requests']:,}"
            if "requests" in settings
            else f"not fixed: {settings.get('duration_s')} s per sample",
        ),
        ("Samples", sampling_text(settings)),
        ("Warm-up", warmup_text(settings)),
        ("Server processes", unique_text([f"{e['settings']['workers']} per stack" for e in envs])),
        *([("Database pool", f"{settings['pool_per_worker']} connections per process")]
          if "pool_per_worker" in settings else []),
        ("CPU placement", unique_text([placement_text(e["settings"]) for e in envs])),
        (
            "Load average at start",
            unique_text([", ".join(f"{v:.2f}" for v in e["loadavg_at_start"]) for e in envs]),
        ),
        ("Database", str(first["postgres"])),
        *([("JavaScript runtimes", runtimes)] if (runtimes := runtimes_text(envs)) else []),
        *[
            ("Command" if len(directories) == 1 else command_label(d, e, multi),
             f"`{run_command(d, e['settings'], multi)}`")
            for d, e in zip(directories, envs, strict=True)
        ],
    ]  # fmt: skip
    body = "\n".join(f"| {name} | {value} |" for name, value in rows)
    return f"| Condition | Value |\n| --- | --- |\n{body}"


def format_warning(directories: list[Path]) -> str:
    tools = {tool_label(read_environment(d)) for d in directories}
    if len(tools) < 2:
        return ""
    return (
        '!!! warning "Different load generators"\n'
        "    These views were sampled with different load generators "
        f"({', '.join(sorted(tools))}); their numbers are not comparable across views."
    )


def reproduce(runs: list[tuple[str, int]], tool: str = "bombardier") -> str:
    """Commands for each (mode, workers) run, then the report over all of them."""
    multi = len({workers for _, workers in runs}) > 1
    modes = [mode for mode, _ in runs]
    commands = "\n".join(
        f"uv run --locked python run.py --server-mode {mode}"
        + (f" --workers {workers}" if multi else "")
        + f" --out {out_dir(mode, workers, multi)}"
        for mode, workers in runs
    )
    outputs = " ".join(out_dir(mode, workers, multi) for mode, workers in runs)
    install = (
        "go install github.com/codesenberg/bombardier@latest\n"
        if tool == "bombardier"
        else ""
    )
    if "javascript" in modes:
        install += "(cd benchmarks/comparative/js && npm ci)  # Node.js and Bun on PATH, see the README\n"
    return (
        f"```sh\n{install}cd benchmarks/comparative\nuv sync --locked\n"
        "VIRTUAL_ENV=$PWD/.venv uvx maturin develop --release --uv --manifest-path ../../crates/bindings/Cargo.toml\n"
        f"{commands}\nuv run --locked python report.py {outputs} > ../../docs/benchmarks.md\n```"
    )


def package_versions(env: JSONData) -> dict[str, str]:
    packages = env.get("packages", [])
    if isinstance(packages, str):  # run.py stores `uv pip list` output as JSON text
        packages = json.loads(packages)
    return {item["name"]: item["version"] for item in packages}


def javascript_versions(env: JSONData) -> dict[str, str]:
    """Runtime and npm package versions a `javascript` run recorded, by display name."""
    recorded = env.get("javascript", {})
    runtimes = {
        RUNTIME_NAMES.get(name, name): version
        for name, version in recorded.get("runtimes", {}).items()
    }
    found = {**recorded.get("packages", {}), **runtimes}
    return {name: version for name, version in found.items() if version}


def all_versions(envs: list[JSONData]) -> dict[str, str]:
    """Every recorded version; the first run that recorded a name wins."""
    merged: dict[str, str] = {}
    for env in envs:
        for name, version in {
            **package_versions(env),
            **javascript_versions(env),
        }.items():
            merged.setdefault(name, version)
    return merged


def runtimes_text(envs: list[JSONData]) -> str:
    """Node.js and Bun versions of the JavaScript runs, or "" without one."""
    versions = all_versions([env for env in envs if "javascript" in env])
    return "; ".join(
        f"{name} {versions[name]}"
        for name in RUNTIME_NAMES.values()
        if name in versions
    )


def indented(text: str, spaces: int = 4) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line else line for line in text.split("\n"))


def sample_end_note(env: JSONData) -> str:
    if tool_name(env) == "oha":
        return "Requests in flight at the end of a sample were aborted by `oha`; they are not counted as unexpected responses."
    stop = (
        "a fixed number of requests"
        if "requests" in env["settings"]
        else "a fixed duration"
    )
    return (
        f"Samples are pooled across server starts and none is discarded. Each sample is {stop}; "
        "`bombardier` counts non-2xx responses and failed requests as unexpected."
    )


def service_table(directories: list[Path]) -> str:
    measured = {
        scenario
        for directory in directories
        for scenario in measured_scenarios(read_environment(directory)["settings"])
    }
    rows = "\n".join(
        f"| `{name}` | `{request}` | {what} |"
        for name, request, what in SERVICE_ROWS
        if name in measured
    )
    return (
        "| Scenario | Request | What the service does |\n| --- | --- | --- |\n" + rows
    )


def stacks_table(envs: list[JSONData], kiso: bool, javascript: bool) -> str:
    versions = all_versions(envs)
    rows = []
    for key, label, orm, server, packages in STACK_ROWS:
        if stack_group(key) == OTHER_RUNTIMES and not javascript:
            continue
        shown = ", ".join(
            f"{name} {versions[name]}" for name in packages if name in versions
        )
        extra = "; or Kiso, Hinoki's own server" if key == "hinoki" and kiso else ""
        rows.append(f"| {label} | {orm} | {server}{extra} | {shown} |")
    return (
        "| Stack | ORM and driver | Server | Versions |\n| --- | --- | --- | --- |\n"
        + "\n".join(rows)
    )


def collapsed(title: str, body: str) -> str:
    return f'??? info "{title}"\n\n{indented(body)}\n'


def session_note(directories: list[Path]) -> str:
    """Which stacks were measured in which run, when the runs differ."""
    runs: dict[str, list[str]] = {}
    for directory in directories:
        env = read_environment(directory)
        labels = [stack_label(s) for s in stack_names(directory, env["settings"])]
        runs.setdefault(str(env["date"])[:16], []).extend(labels)
    if len(runs) < 2:
        return ""
    parts = "; ".join(f"{when}: {', '.join(labels)}" for when, labels in runs.items())
    return (
        f"The stacks were measured in {len(runs)} separate runs on the same machine ({parts}). "
        "The host load was not controlled between runs, so read a difference between stacks from different runs with that in mind."
    )


def lead(groups: dict[int, list[Path]], rows: Rows) -> str:
    first_dir = next(iter(groups.values()))[0]
    first = read_environment(first_dir)
    dates = " and ".join(
        dict.fromkeys(
            str(read_environment(d)["date"])[:10]
            for dirs in groups.values()
            for d in dirs
        )
    )
    stacks = {stack for stacks in rows.values() for stack in stacks}
    settings = first["settings"]
    return (
        '<div class="bench-lead">'
        f'<p class="bench-lead__what"><b>Measured:</b> requests per second and latency of {len(stacks)} stack setups '
        f"on {len(rows)} request types of one small API backed by PostgreSQL, "
        f"with {worker_list(list(groups))}. "
        f"<b>Where:</b> {html.escape(machine_text(first))}, {dates}, "
        f"{settings['concurrency']} concurrent connections, median of {total_samples(settings)} samples per cell.</p>"
        '<p class="bench-lead__caveat">Pre-alpha software, one host, warm data, loopback network: '
        "this is not a ranking and not a production claim.</p></div>"
    )


def worker_name(workers: int) -> str:
    return f"{workers} worker{'s' if workers != 1 else ''}"


def worker_kind(workers: int) -> str:
    return "single-process" if workers == 1 else "multi-process"


def worker_list(counts: list[int]) -> str:
    return " and ".join(f"{worker_name(n)} ({worker_kind(n)})" for n in counts)


KISO_NOTE = (
    "Hinoki (Kiso) is Hinoki's own Rust server and its default (started with hinoki serve --server kiso), "
    "as Actix is Django Bolt's; Hinoki (Granian) runs the same application on Granian, a third-party ASGI server. "
    "Every other framework runs on the server its documentation uses."
)
SAME_GRANIAN_NOTE = (
    "Hinoki, Litestar, FastAPI and Django Ninja run on one Granian version and configuration, "
    "which takes the server out of the comparison. Django Bolt is not here because it does not expose ASGI."
)
SUMMARY_ROUTES = ("json-1k", "db", "auth-articles")
SUMMARY_NAMES = {
    "json-1k": "JSON 1 KB",
    "db": "10 rows from PostgreSQL",
    "auth-articles": "Articles with JWT auth (ORM)",
}


def against_text(verdict: str, other: str) -> str:
    """What follows the figure and its word: "than X" for a ranking, "with X" otherwise."""
    name = stack_label(other)
    return f" than {name}" if verdict in ("higher", "lower") else f" with {name}"


def summary_cell(
    ref_row: JSONData, other_row: JSONData, other: str
) -> report_html.SummaryCell:
    verdict, phrase = standing(ref_row, other_row)
    value, _, word = phrase.partition(" ")
    if verdict == "tie":
        value, word = "Tie", f"within {TIE_PCT}%"
    elif verdict == "unsettled":
        value, word = "Not settled", f"ranges overlap or CV > {STABLE_CV}%"
    return report_html.SummaryCell(verdict, value, word, against_text(verdict, other))


def summary_columns(rows: Rows, ref: str) -> list[str]:
    """Every competitor in a summary route that has the reference, in stack order."""
    return sorted(
        {
            key
            for route in SUMMARY_ROUTES
            if ref in rows.get(route, {})
            for key in rows[route]
            if key != ref and key not in GRANIAN_KEYS
        },
        key=stack_rank,
    )


def summary_rows(
    rows: Rows, ref: str, columns: list[str]
) -> list[report_html.SummaryRow]:
    table = []
    for route in SUMMARY_ROUTES:
        stacks = rows.get(route)
        if not stacks or ref not in stacks:
            continue
        table.append(
            report_html.SummaryRow(
                SUMMARY_NAMES[route],
                rps(stacks[ref]["median"]),
                [
                    summary_cell(stacks[ref], stacks[key], key)
                    if key in stacks
                    else None
                    for key in columns
                ],
            )
        )
    return table


def summary_block(rows: Rows, workers: int, settings: JSONData) -> str:
    ref = reference_key(rows)
    columns = summary_columns(rows, ref)
    finding = lead_finding(rows)
    conditions = f"{worker_name(workers)}, {settings['concurrency']} connections"
    return (
        report_html.summary_table(
            [(STACKS[key].framework, STACKS[key].server) for key in columns],
            summary_rows(rows, ref, columns),
            (STACKS[ref].framework, STACKS[ref].server),
            conditions,
            TIE_PCT,
        )
        + (f'<p class="bench-reading">{html.escape(finding)}</p>' if finding else "")
        + '<p class="bench-reading">Each result is the median of '
        f"{total_samples(settings)} samples. With so few samples a gap can be noise, "
        "so a gap under 10% is a tie and a larger one counts only when the sample ranges do not overlap.</p>"
    )


def view_block(view: str, title: str, intro: list[str], rows: Rows, group: str) -> str:
    prefix = f"bench-{group}-{view}"
    return (
        f'<section class="bench-view" id="{prefix}"><h3>{html.escape(title)}</h3>'
        + "".join(
            f'<p class="bench-reading">{html.escape(text)}</p>'
            for text in intro
            if text
        )
        + report_html.explorer(make_scenarios(rows), prefix)
        + "</section>"
    )


def dirs_with(dirs: list[Path], *modes: str) -> list[Path]:
    return [d for d in dirs if server_mode(d) in modes]


def worker_views(dirs: list[Path], workers: int) -> str:
    """The summary and the three views (Python, same server, other runtimes) of one worker count."""
    group = f"w{workers}"
    settings = read_environment(dirs[0])["settings"]
    python = dirs_with(dirs, "documented", "kiso")
    same = dirs_with(dirs, "same-granian")
    other = [*dirs_with(dirs, "kiso"), *dirs_with(dirs, "javascript")]
    parts = []
    if python:
        rows, notes = collect(python)
        parts.append(
            f'<section class="bench-view" id="bench-{group}-summary"><h3>Summary</h3>'
            f"{summary_block(rows, workers, settings)}</section>"
        )
        intro = [
            KISO_NOTE if versus_keys(rows) else "",
            session_note(python),
            *notes,
        ]
        parts.append(view_block("python", "Python frameworks", intro, rows, group))
    if same:
        rows, notes = collect(same)
        parts.append(
            view_block(
                "same-granian",
                "Same ASGI server (Granian)",
                [SAME_GRANIAN_NOTE, *notes],
                rows,
                group,
            )
        )
    if dirs_with(other, "javascript"):
        rows, notes = collect(other)
        parts.append(
            view_block(
                "other-runtimes",
                "Other runtimes (Bun and Node.js)",
                [OTHER_RUNTIMES_NOTE, *notes],
                rows,
                group,
            )
        )
    return "".join(parts)


def group_by_workers(directories: list[Path]) -> dict[int, list[Path]]:
    """Directories by their recorded worker count, most workers first."""
    groups: dict[int, list[Path]] = {}
    for directory in directories:
        count = int(read_environment(directory)["settings"]["workers"])
        groups.setdefault(count, []).append(directory)
    return dict(sorted(groups.items(), reverse=True))


def results_section(groups: dict[int, list[Path]]) -> str:
    """One block per worker count, behind top-level tabs when there are several."""
    heading = {n: f"Results with {worker_list([n])}" for n in groups}
    if len(groups) == 1:
        ((count, dirs),) = groups.items()
        return f"<h2>{html.escape(heading[count])}</h2>" + worker_views(dirs, count)
    labels = [
        (f"w{n}", f"{worker_name(n)} <small>{worker_kind(n)}</small>") for n in groups
    ]
    bar = report_html.tabs(
        labels, "bench-workers", "Server processes", " bench-tabs--workers"
    )
    panels = "".join(
        f'<section class="bench-workerpanel" role="tabpanel" id="bench-workers-w{n}-panel" '
        f'aria-labelledby="bench-workers-w{n}-tab" data-selected="{str(index == 0).lower()}">'
        f"<h2>{html.escape(heading[n])}</h2>{worker_views(dirs, n)}</section>"
        for index, (n, dirs) in enumerate(groups.items())
    )
    return f'<div class="bench-explorer bench-workers" data-bench-explorer>{bar}{panels}</div>'


def method_section(directories: list[Path], first_env: JSONData) -> str:
    runs = [
        (server_mode(d), int(read_environment(d)["settings"]["workers"]))
        for d in directories
    ]
    locations = ", ".join(
        sorted({comparative_results_reference(result_date(d)) for d in directories})
    )
    kiso = any(
        KISO_KEY in stack_names(d, read_environment(d)["settings"]) for d in directories
    )
    javascript = any(
        stack_group(stack) == OTHER_RUNTIMES
        for d in directories
        for stack in stack_names(d, read_environment(d)["settings"])
    )
    legacy = any(tool_name(read_environment(d)) == "oha" for d in directories)
    warning = format_warning(directories)
    verified = (
        "All listed stacks passed the response and SQL statement-count verifier before measurement. "
        + sample_end_note(first_env)
    )
    return "\n".join(
        [
            "## Method and limits\n",
            collapsed(
                "Conditions",
                conditions_table(directories)
                + ("\n\n" + warning if warning else "")
                + ("\n\n" + LEGACY_NOTE if legacy else ""),
            ),
            collapsed(
                "Workloads, stacks and versions",
                "One service with Django Bolt's seven routes, its tables and deterministic seed data: "
                f"{parity_data.USERS} users, {parity_data.AUTHORS} authors, {parity_data.TAGS} tags, "
                f"{parity_data.ARTICLES:,} articles and {parity_data.COMMENTS:,} comments, plus the user of the JWT. "
                "Every stack had to match a PostgreSQL oracle and its expected SQL statement count per request before measurement "
                "(one extra statement on the tag routes for Hinoki, none elsewhere). "
                "The stacks differ in ORM, database driver and server, as listed below.\n\n"
                + service_table(directories)
                + "\n\n"
                + stacks_table(
                    [read_environment(d) for d in directories], kiso, javascript
                ),
            ),
            collapsed(
                "How the numbers are computed",
                verified + "\n\n" + "\n".join(f"- {note}" for note in READING_NOTES),
            ),
            collapsed(
                "Reproduce",
                reproduce(runs, tool_name(first_env))
                + "\n\n"
                + f"Raw `{tool_name(first_env)}` JSON, environment records, server logs, per-worker CPU time and verification output are kept in {locations}. "
                "Setup details, the database container and the rules for repeating an unstable scenario are in `benchmarks/comparative/README.md`.",
            ),
        ]
    )


def main(directories: list[Path]) -> None:
    groups = group_by_workers(directories)
    first_env = read_environment(directories[0])
    every, _ = collect(
        [
            d
            for dirs in groups.values()
            for d in dirs
            if server_mode(d) != "same-granian"
        ]
        or directories
    )
    parts = [
        f'<p class="bench-eyebrow">FIELD NOTE · {html.escape(str(first_env["date"]))} · {html.escape(first_env["machine"])}</p>\n',
        "# Benchmarks\n",
        lead(groups, every) + "\n",
        results_section(groups),
        method_section(directories, first_env),
    ]
    print("\n".join(part for part in parts if part).rstrip("\n"))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: report.py RESULTS_DIR [RESULTS_DIR ...]")
    main([Path(arg) for arg in sys.argv[1:]])
