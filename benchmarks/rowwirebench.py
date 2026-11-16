"""Measure the generated CRUD RowPlan against its Python oracle path.

Verification runs before every timed campaign. Raw Oha output, process logs and
the report stay under the ignored ``.cache`` directory; no public results are
rewritten by this script.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import importlib.util
import json
import os
import platform
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmarks.rowwireapp import DATABASE_URL, make_app  # noqa: E402
from hinoki.db import Database  # noqa: E402
from tests.harness import Client, Feed, Sink  # noqa: E402

OHA = shutil.which("oha")
GRANIAN_AVAILABLE = importlib.util.find_spec("granian") is not None
HOST = "127.0.0.1"
TRANSIENT_HEADERS = {b"date", b"x-request-id"}
MAX_PAGE_SIZE = 100
HTTP_OK = 200
QUERY_COUNT = 2
REQUIRED_RUNS = 5
REGRESSION_THRESHOLD_PCT = 15.0
SPREAD_MULTIPLIER = 2.0
MAX_LOAD_AVERAGE = 1.0
QUIET_STABLE_SECONDS = 10.0
QUIET_POLL_SECONDS = 2.0
QUIET_TIMEOUT_SECONDS = 300.0


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--duration", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--pool-size", type=int, default=10)
    parser.add_argument("--connections", type=int, default=64)
    parser.add_argument("--page-sizes", type=int, nargs="+", default=[20, 100])
    parser.add_argument("--seed-rows", type=int, default=10_000)
    parser.add_argument("--port", type=int, default=8891)
    parser.add_argument("--server-cpus", default="0-3")
    parser.add_argument("--load-cpus", default="6-7")
    parser.add_argument("--out", type=Path, default=ROOT / ".cache/rowwire-ab")
    args = parser.parse_args()
    if (
        min(args.runs, args.duration, args.workers, args.pool_size, args.connections)
        < 1
    ):
        parser.error(
            "runs, duration, workers, pool-size and connections must be positive"
        )
    if args.warmup < 0 or args.seed_rows < max(args.page_sizes):
        parser.error(
            "warmup must be non-negative and seed rows must cover the largest page"
        )
    if any(size < 1 or size > MAX_PAGE_SIZE for size in args.page_sizes):
        parser.error("page sizes must be between 1 and the default maximum of 100")
    return args


def command_output(command: list[str]) -> str:
    try:
        result = subprocess.run(  # noqa: S603 - commands use fixed tools and local arguments
            command, check=False, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    return result.stdout.strip() or "unavailable"


def git_status() -> str:
    git = shutil.which("git")
    if git is None:
        raise SystemExit("git is required to record benchmark source state")
    result = subprocess.run(  # noqa: S603 - fixed local git status command
        [git, "-C", str(ROOT), "status", "--porcelain"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode:
        raise SystemExit("could not determine whether the benchmark worktree is clean")
    return result.stdout.strip()


def ensure_clean_for_timing(status: str, *, verify_only: bool) -> None:
    if status and not verify_only:
        raise SystemExit(
            "timed campaigns require a clean committed worktree; commit or remove "
            "changes before measuring"
        )


def cpu_model() -> str:
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    return platform.processor() or "unknown"


def pin(cpus: str) -> list[str]:
    return (
        ["taskset", "-c", cpus]
        if sys.platform == "linux" and shutil.which("taskset")
        else []
    )


def requested_cpus(value: str) -> set[int]:
    cpus: set[int] = set()
    for part in value.split(","):
        bounds = part.split("-", maxsplit=1)
        cpus.update(range(int(bounds[0]), int(bounds[-1]) + 1))
    return cpus


def verify_cpu_sets(args: argparse.Namespace) -> None:
    if not hasattr(os, "sched_getaffinity") or not pin(args.server_cpus):
        return
    allowed = os.sched_getaffinity(0)
    for label, value in (
        ("server", args.server_cpus),
        ("load generator", args.load_cpus),
    ):
        requested = requested_cpus(value)
        if not requested <= allowed:
            raise SystemExit(
                f"requested {label} CPUs {sorted(requested)} outside allowed set "
                f"{sorted(allowed)}"
            )


def load_average() -> list[float] | None:
    try:
        return list(os.getloadavg())
    except OSError:
        return None


def process_snapshot() -> list[str]:
    listing = command_output(["ps", "-eo", "pid,comm,pcpu", "--sort=-pcpu"])
    return listing.splitlines()[1:11]


def wait_for_quiet_host() -> dict[str, Any]:
    started = time.monotonic()
    quiet_since: float | None = None
    maximum_load = 0.0
    polls = 0
    latest: list[float] | None = None
    while True:
        latest = load_average()
        polls += 1
        if latest:
            maximum_load = max(maximum_load, latest[0])
        now = time.monotonic()
        if not latest or latest[0] <= MAX_LOAD_AVERAGE:
            if quiet_since is None:
                quiet_since = now
            if now - quiet_since >= QUIET_STABLE_SECONDS:
                return {
                    "load_average": latest,
                    "wait_seconds": round(now - started, 1),
                    "polls": polls,
                    "max_load_while_waiting": maximum_load,
                    "active_processes": process_snapshot(),
                }
        else:
            quiet_since = None
        elapsed = now - started
        if elapsed >= QUIET_TIMEOUT_SECONDS:
            raise RuntimeError(
                "host did not stay quiet for 10 seconds within 5 minutes; "
                f"last_load={latest}, max_load={maximum_load}, "
                f"active_processes={process_snapshot()}"
            )
        time.sleep(QUIET_POLL_SECONDS)


def ensure_port_available(port: int) -> None:
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((HOST, port))
        except OSError as error:
            raise SystemExit(f"benchmark port {port} is unavailable") from error


def ensure_empty_output(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise SystemExit(
            f"benchmark output directory is not empty: {path}; choose a new path"
        )
    path.mkdir(parents=True, exist_ok=True)


async def seed_database(row_count: int) -> None:
    database = await Database.connect(DATABASE_URL, max_size=1)
    try:
        await database.execute("DROP TABLE IF EXISTS row_wire_bench_records")
        await database.execute(
            "CREATE TABLE row_wire_bench_records ("
            "id integer PRIMARY KEY, title text NOT NULL, count bigint NOT NULL, "
            "ratio double precision NOT NULL, active boolean NOT NULL, "
            "created_at timestamptz NOT NULL, nullable text NULL, "
            "hidden integer NOT NULL)"
        )
        await database.execute(
            "INSERT INTO row_wire_bench_records "
            "SELECT n, 'record-' || n, n::bigint, n::double precision / 10, "
            "(n % 2 = 0), timestamptz '2026-01-01 00:00:00+00' "
            "+ n * interval '1 second', "
            "CASE WHEN n % 3 = 0 THEN NULL ELSE 'value-' || n END, n * 7 "
            "FROM generate_series(1, $1::integer) AS rows(n)",
            row_count,
        )
    finally:
        database.close()


async def postgres_version() -> str:
    database = await Database.connect(DATABASE_URL, max_size=1)
    try:
        rows = await database.fetch("SELECT version() AS version")
        return str(rows[0]["version"])
    finally:
        database.close()


async def start_client(variant: str, args: argparse.Namespace) -> Client:
    client = Client(
        make_app(
            variant,
            database_url=DATABASE_URL,
            pool_size=args.pool_size,
        ).asgi()
    )
    if await client.lifespan_event("startup") != "lifespan.startup.complete":
        raise RuntimeError("ASGI startup failed")
    return client


async def raw_asgi(
    client: Client, method: str, page_size: int
) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    path = "/records"
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": f"page=1&page_size={page_size}".encode(),
        "headers": [(b"host", b"localhost"), (b"accept", b"application/json")],
    }
    sink = Sink()
    await client.adapter(
        scope, Feed({"type": "http.request", "body": b"", "more_body": False}), sink
    )
    start, end = sink.sent
    headers = [
        (name, value)
        for name, value in start["headers"]
        if name not in TRANSIENT_HEADERS
    ]
    return int(start["status"]), headers, end["body"]


def request_profile(frame: Any, event: str, _arg: object) -> None:
    if event != "call":
        return
    name = frame.f_code.co_name
    filename = frame.f_code.co_filename
    if name == "from_row" and filename.endswith("/orm/model.py"):
        PROFILE["model_from_row"] += 1
    elif name == "to_schema" and filename.endswith("/crud.py"):
        PROFILE["to_schema"] += 1
    elif name == "__init__" and filename == "<string>":
        PROFILE["generated_init"] += 1


PROFILE: Counter[str] = Counter()


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


class TrackedDatabaseCall:
    def __init__(self, kind: str, original: Any, events: list[tuple[str, str]]) -> None:
        self.kind = kind
        self.original = original
        self.events = events

    def __get__(self, instance: object | None, owner: type | None = None) -> Any:
        del owner
        if instance is None:
            return self
        return functools.partial(self.call, instance)

    async def call(self, instance: object, sql: str, *args: Any) -> Any:
        self.events.append((self.kind, sql))
        return await self.original(instance, sql, *args)


async def inspect_variant(
    variant: str, page_size: int, args: argparse.Namespace
) -> dict[str, Any]:
    app_client = await start_client(variant, args)
    events: list[tuple[str, str]] = []
    PROFILE.clear()
    try:
        with (
            patch.object(
                Database,
                "fetch",
                TrackedDatabaseCall("fetch", Database.fetch, events),
            ),
            patch.object(
                Database,
                "_fetch_page_wire",
                TrackedDatabaseCall("wire", Database._fetch_page_wire, events),
            ),
        ):
            sys.setprofile(request_profile)
            result = await raw_asgi(app_client, "GET", page_size)
            sys.setprofile(None)
        return {
            "response": result,
            "events": events,
            "profile": dict(PROFILE),
        }
    finally:
        sys.setprofile(None)
        await app_client.lifespan_event("shutdown")


async def verify_in_process(
    page_sizes: list[int], args: argparse.Namespace
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for page_size in page_sizes:
        wire = await inspect_variant("wire", page_size, args)
        python = await inspect_variant("python", page_size, args)
        validate_preflight(page_size, wire, python)
        wire_head = await head_in_process("wire", page_size, args)
        python_head = await head_in_process("python", page_size, args)
        ensure(
            wire_head == python_head and wire_head[0] == HTTP_OK and not wire_head[2],
            f"wire/Python HEAD differs for page size {page_size}",
        )
        results.append(
            {
                "page_size": page_size,
                "wire": summarize_preflight(wire),
                "python": summarize_preflight(python),
            }
        )
    return results


def validate_preflight(
    page_size: int, wire: dict[str, Any], python: dict[str, Any]
) -> None:
    ensure(
        wire["response"] == python["response"], f"GET differs at page size {page_size}"
    )
    ensure(
        wire["response"][0] == HTTP_OK and bool(wire["response"][2]),
        f"GET verification failed for page size {page_size}",
    )
    ensure(sum(kind == "wire" for kind, _ in wire["events"]) == 1, "wire fetch missing")
    ensure(
        not any(kind == "wire" for kind, _ in python["events"]),
        "control used wire fetch",
    )
    ensure(
        len(wire["events"]) == QUERY_COUNT and len(python["events"]) == QUERY_COUNT,
        "expected one count query and one page query per request",
    )
    ensure(
        [sql for _, sql in wire["events"]] == [sql for _, sql in python["events"]],
        "wire/Python SQL sequences differ",
    )
    ensure(
        not wire["profile"].get("model_from_row", 0)
        and not wire["profile"].get("to_schema", 0),
        "wire path materialized Python rows or schemas",
    )
    for method in ("model_from_row", "to_schema"):
        ensure(
            python["profile"].get(method) == page_size,
            f"Python control did not call {method} once per requested row",
        )


def summarize_preflight(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": result["response"][0],
        "query_kinds": [kind for kind, _sql in result["events"]],
        "profile": result["profile"],
    }


async def head_in_process(
    variant: str, page_size: int, args: argparse.Namespace
) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    client = await start_client(variant, args)
    try:
        return await raw_asgi(client, "HEAD", page_size)
    finally:
        await client.lifespan_event("shutdown")


def response_from_url(
    url: str, method: str
) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    request = urllib.request.Request(  # noqa: S310 - only loopback HTTP URLs are passed
        url, method=method, headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - URL is fixed to loopback HTTP
            request, timeout=10
        ) as response:
            status, headers, body = response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        status, headers, body = error.code, error.headers, error.read()
    normalized = sorted(
        (name.lower().encode(), value.encode())
        for name, value in headers.items()
        if name.lower().encode() not in TRANSIENT_HEADERS
    )
    return status, normalized, body


def server_environment(variant: str, args: argparse.Namespace) -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": f"{ROOT / 'benchmarks'}:{ROOT}",
        "ROWWIRE_DATABASE_URL": DATABASE_URL,
        "ROWWIRE_VARIANT": variant,
        "ROWWIRE_POOL_SIZE": str(args.pool_size),
    }


def start_server(
    variant: str, args: argparse.Namespace, output: Path
) -> subprocess.Popen[bytes]:
    command = [
        sys.executable,
        "-m",
        "granian",
        "--interface",
        "asgi",
        "--workers",
        str(args.workers),
        "--port",
        str(args.port),
        "--no-log",
        "rowwireapp:app",
    ]
    return subprocess.Popen(  # noqa: S603 - executable and target are fixed
        [*pin(args.server_cpus), *command],
        cwd=ROOT / "benchmarks",
        env=server_environment(variant, args),
        stdout=output.open("wb"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def wait_ready(port: int, timeout: float = 30) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            response_from_url(f"http://{HOST}:{port}/records?page=1&page_size=1", "GET")
            return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("Granian did not become ready")


def stop_server(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def verify_server_variants(
    args: argparse.Namespace, output: Path
) -> list[dict[str, Any]]:
    results = []
    for page_size in args.page_sizes:
        replies: dict[str, dict[str, Any]] = {}
        for variant in ("wire", "python"):
            asyncio.run(seed_database(args.seed_rows))
            log = output / f"verify-{variant}.log"
            process = start_server(variant, args, log)
            try:
                wait_ready(args.port)
                replies[variant] = {
                    method: response_from_url(
                        f"http://{HOST}:{args.port}/records?page=1&page_size={page_size}",
                        method,
                    )
                    for method in ("GET", "HEAD")
                }
            finally:
                stop_server(process)
                time.sleep(0.5)
        if replies["wire"] != replies["python"]:
            raise RuntimeError(
                f"live Granian response mismatch at page size {page_size}"
            )
        if (
            replies["wire"]["GET"][0] != HTTP_OK
            or replies["wire"]["HEAD"][0] != HTTP_OK
        ):
            raise RuntimeError(
                f"live Granian verification failed at page size {page_size}"
            )
        results.append(
            {
                "page_size": page_size,
                "status": replies["wire"]["GET"][0],
                "get_bytes": len(replies["wire"]["GET"][2]),
                "head_bytes": len(replies["wire"]["HEAD"][2]),
            }
        )
    return results


def oha_command(url: str, args: argparse.Namespace) -> list[str]:
    if OHA is None:
        raise SystemExit("oha is required for timed runs")
    return [
        *pin(args.load_cpus),
        OHA,
        "-z",
        f"{args.duration}s",
        "-c",
        str(args.connections),
        "--no-tui",
        "--output-format",
        "json",
        "-m",
        "GET",
        url,
    ]


def metrics(raw: dict[str, Any]) -> dict[str, float]:
    return {
        "requests_per_second": float(raw["summary"]["requestsPerSec"]),
        "p50_ms": float(raw["latencyPercentiles"]["p50"]) * 1000,
        "p99_ms": float(raw["latencyPercentiles"]["p99"]) * 1000,
    }


def sample_stats(values: list[float]) -> dict[str, float | int | None] | None:
    if not values:
        return None
    mean = statistics.mean(values)
    return {
        "sample_count": len(values),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "cv_pct": statistics.stdev(values) / mean * 100
        if len(values) > 1 and mean
        else None,
    }


METRIC_NAMES = ("requests_per_second", "p50_ms", "p99_ms")


def variant_report(
    samples: list[dict[str, Any]], page_size: int, variant: str
) -> dict[str, dict[str, float | int | None] | None]:
    selected = [
        sample
        for sample in samples
        if sample["page_size"] == page_size and sample["variant"] == variant
    ]
    return {
        metric: sample_stats([sample[metric] for sample in selected])
        for metric in METRIC_NAMES
    }


def metric_comparison(
    samples: list[dict[str, Any]], page_size: int, metric: str
) -> dict[str, Any]:
    paired = {
        sample["run"]: sample[metric]
        for sample in samples
        if sample["page_size"] == page_size and sample["variant"] == "wire"
    }
    reference = {
        sample["run"]: sample[metric]
        for sample in samples
        if sample["page_size"] == page_size and sample["variant"] == "python"
    }
    changes = [
        (paired[index] - reference[index]) / reference[index] * 100
        for index in sorted(paired.keys() & reference.keys())
        if reference[index]
    ]
    median_change = statistics.median(changes) if changes else None
    spread = statistics.stdev(changes) if len(changes) > 1 else None
    signal = (
        len(changes) >= REQUIRED_RUNS
        and median_change is not None
        and spread is not None
        and abs(median_change) > REGRESSION_THRESHOLD_PCT
        and abs(median_change) > SPREAD_MULTIPLIER * spread
    )
    return {
        "paired_sample_count": len(changes),
        "paired_change_pct": changes,
        "median_change_pct": median_change,
        "paired_change_stdev_pct": spread,
        "detector_signal": signal,
        "interpretation": (
            "insufficient paired samples"
            if len(changes) < REQUIRED_RUNS
            else "outside the 15% and 2x paired-spread investigation policy"
            if signal
            else "no result beyond the 15% and 2x paired-spread policy"
        ),
    }


def build_report(
    samples: list[dict[str, Any]], page_sizes: tuple[int, ...] | list[int] = ()
) -> dict[str, Any]:
    pages = sorted({*page_sizes, *(sample["page_size"] for sample in samples)})
    variants = {
        str(page): {
            variant: variant_report(samples, page, variant)
            for variant in ("wire", "python")
        }
        for page in pages
    }
    comparisons = {
        str(page): {
            metric: metric_comparison(samples, page, metric) for metric in METRIC_NAMES
        }
        for page in pages
    }
    return {"variants": variants, "comparison": comparisons}


def warmup(url: str, args: argparse.Namespace) -> None:
    if not args.warmup:
        return
    assert OHA is not None
    subprocess.run(  # noqa: S603 - OHA and arguments are fixed/validated
        [
            *pin(args.load_cpus),
            OHA,
            "-z",
            f"{args.warmup}s",
            "-c",
            str(args.connections),
            "--no-tui",
            url,
        ],
        check=True,
        capture_output=True,
    )


def collect_sample(
    variant: str,
    page_size: int,
    run: int,
    order: tuple[str, str],
    args: argparse.Namespace,
    output: Path,
) -> dict[str, Any]:
    quiet_host = wait_for_quiet_host()
    host_load = quiet_host["load_average"]
    asyncio.run(seed_database(args.seed_rows))
    process = start_server(variant, args, output / f"{variant}-{page_size}-{run}.log")
    try:
        wait_ready(args.port)
        url = f"http://{HOST}:{args.port}/records?page=1&page_size={page_size}"
        warmup(url, args)
        before = load_average()
        started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        result = subprocess.run(  # noqa: S603 - OHA command uses validated options
            oha_command(url, args),
            check=True,
            capture_output=True,
            text=True,
        )
        after = load_average()
        raw = json.loads(result.stdout)
        raw_path = output / f"{variant}-page-{page_size}-run-{run}.json"
        raw_path.write_text(json.dumps(raw, indent=2) + "\n")
        return {
            "variant": variant,
            "page_size": page_size,
            "run": run,
            "order": list(order),
            "started_at": started,
            "host_load_before_start": host_load,
            "quiet_host": quiet_host,
            "load_before": before,
            "load_after": after,
            "server_affinity": command_output(["taskset", "-cp", str(process.pid)])
            if pin(args.server_cpus)
            else "unpinned",
            "raw_file": raw_path.name,
            **metrics(raw),
        }
    finally:
        stop_server(process)
        time.sleep(0.5)


def run_measurements(
    args: argparse.Namespace, output: Path, samples: list[dict[str, Any]]
) -> None:
    for page_size in args.page_sizes:
        for index in range(args.runs):
            order = ("wire", "python") if index % 2 == 0 else ("python", "wire")
            for variant in order:
                sample = collect_sample(
                    variant, page_size, index + 1, order, args, output
                )
                samples.append(sample)
                print(
                    f"{variant} page={page_size} run={index + 1}: "
                    f"{sample['requests_per_second']:.1f} req/s"
                )


def main() -> None:
    args = arguments()
    status = git_status()
    ensure_clean_for_timing(status, verify_only=args.verify_only)
    if not GRANIAN_AVAILABLE:
        raise SystemExit("Granian must be installed in the active environment")
    if not args.verify_only and OHA is None:
        raise SystemExit("oha must be installed for timed runs")
    ensure_port_available(args.port)
    verify_cpu_sets(args)
    ensure_empty_output(args.out)
    load_at_start = load_average()
    metadata: dict[str, Any] = {
        "commit": command_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"]),
        "worktree_clean": not status,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "python": sys.version,
        "rustc": command_output(["rustc", "--version"]),
        "granian": command_output([sys.executable, "-m", "granian", "--version"]),
        "oha": command_output([OHA, "--version"]) if OHA else "not used",
        "platform": platform.platform(),
        "cpu_model": cpu_model(),
        "cpu_count": os.cpu_count(),
        "load_average_start": load_at_start,
        "database": database_identity(DATABASE_URL),
        "settings": vars(args) | {"out": str(args.out)},
    }
    metadata["postgres"] = asyncio.run(postgres_version())
    (args.out / "environment.json").write_text(
        json.dumps(metadata, indent=2, default=str) + "\n"
    )
    asyncio.run(seed_database(args.seed_rows))
    in_process = asyncio.run(verify_in_process(args.page_sizes, args))
    live = verify_server_variants(args, args.out)
    report: dict[str, Any] = {
        "verification": {"in_process": in_process, "granian": live},
        "samples": [],
    }
    if not args.verify_only:
        try:
            run_measurements(args, args.out, report["samples"])
        except Exception as error:
            report["error"] = f"{type(error).__name__}: {error}"
            report["summary"] = build_report(report["samples"], args.page_sizes)
            report["load_average_end"] = load_average()
            (args.out / "summary.json").write_text(
                json.dumps(report, indent=2, default=str) + "\n"
            )
            raise
        report["summary"] = build_report(report["samples"], args.page_sizes)
    report["load_average_end"] = load_average()
    (args.out / "summary.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n"
    )
    print(json.dumps(report, indent=2, default=str))


def database_identity(url: str) -> dict[str, str | None]:
    parsed = urlsplit(url)
    return {
        "scheme": parsed.scheme,
        "host": parsed.hostname,
        "port": str(parsed.port) if parsed.port else None,
        "database": parsed.path.lstrip("/"),
    }


if __name__ == "__main__":
    main()
