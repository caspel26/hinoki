"""Run paired end-to-end orientation samples for native generated CRUD."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import platform
import re
import signal
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from archive_refs import phase1_results_reference
from phase1_metrics import request_count
from phase1_seed import BASE_TITLE, BODY, FIXED_ID, seed

HERE = Path(__file__).parent
DB = os.environ.get(
    "PHASE1_DATABASE_URL", "postgres://postgres:hinoki@127.0.0.1:32779/hinoki"
)
CONTAINER = os.environ.get(
    "PHASE1_POSTGRES_CONTAINER", "hinoki-rust-crud-step7-20261118-pg"
)
WORKERS = 4
POOL = 10
CONCURRENCY = 64
SERVER_CPUS = "0-3"
LOAD_CPUS = "6-7"
SCENARIOS = ("list_20", "list_100", "detail", "create", "update")
CREATE_TITLE = "Orientation create"
CREATE_BODY = {"title": CREATE_TITLE, "body": BODY}
UPDATE_TITLE = "Orientation updated"
UPDATE_BODY = {"title": UPDATE_TITLE}


def execute(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True, **kwargs)


def initialize_database() -> None:
    with psycopg.connect(DB, autocommit=True) as connection:
        connection.execute((HERE / "phase1-schema.sql").read_text())
    seed(DB)


def sample_database_state() -> None:
    with psycopg.connect(DB, autocommit=True) as connection:
        connection.execute("DELETE FROM phase1_posts WHERE title = %s", (CREATE_TITLE,))
        connection.execute(
            "UPDATE phase1_posts SET title = %s WHERE id = %s",
            (BASE_TITLE, FIXED_ID),
        )


def scenario_request(name: str) -> tuple[str, str, dict[str, str] | None]:
    if name.startswith("list_"):
        return "GET", f"/crud-posts?page=1&page_size={name.removeprefix('list_')}", None
    if name == "detail":
        return "GET", f"/crud-posts/{FIXED_ID}", None
    if name == "create":
        return "POST", "/crud-posts", CREATE_BODY
    return "PATCH", f"/crud-posts/{FIXED_ID}", UPDATE_BODY


def request(
    base: str, method: str, path: str, body: dict[str, str] | None = None
) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def assert_fixture(base: str) -> dict[str, Any]:
    outputs: dict[str, Any] = {}
    for size in (20, 100):
        status, payload = request(base, "GET", f"/crud-posts?page=1&page_size={size}")
        assert status == 200 and len(payload["items"]) == size
        assert (payload["page"], payload["page_size"], payload["total"]) == (
            1,
            size,
            10_000,
        )
        outputs[f"list_{size}"] = payload
    status, detail = request(base, "GET", f"/crud-posts/{FIXED_ID}")
    assert status == 200 and detail == {
        "id": str(FIXED_ID),
        "title": BASE_TITLE,
        "body": BODY,
    }
    outputs["detail"] = detail
    status, created = request(base, "POST", "/crud-posts", CREATE_BODY)
    assert status == 201 and created["title"] == CREATE_TITLE
    assert created["body"] == BODY
    with psycopg.connect(DB) as connection:
        assert connection.execute(
            "SELECT title, body FROM phase1_posts WHERE id = %s", (created["id"],)
        ).fetchone() == (CREATE_TITLE, BODY)
    outputs["create"] = {
        "status": status,
        "title": created["title"],
        "body": created["body"],
    }
    status, updated = request(base, "PATCH", f"/crud-posts/{FIXED_ID}", UPDATE_BODY)
    assert status == 200 and updated == {
        "id": str(FIXED_ID),
        "title": UPDATE_TITLE,
        "body": BODY,
    }
    outputs["update"] = updated
    sample_database_state()
    return outputs


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def server_command(framework: str, port: int) -> tuple[list[str], dict[str, str]]:
    taskset = ["taskset", "-c", SERVER_CPUS] if platform.system() == "Linux" else []
    if framework == "hinoki":
        env = {"PHASE1_DATABASE__URL": DB, "PHASE1_DATABASE__MAX_SIZE": str(POOL)}
        command = [
            sys.executable,
            "-m",
            "hinoki",
            "serve",
            "phase1_hinoki:app",
            "--server",
            "kiso",
            "--workers",
            str(WORKERS),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]
    else:
        env = {
            "BENCH_DATABASE_URL": DB,
            "BENCH_POOL": str(POOL),
            "BENCH_BOLT": "1",
            "DJANGO_SETTINGS_MODULE": "dj_settings",
        }
        command = [
            sys.executable,
            "manage.py",
            "runbolt",
            "--host",
            "127.0.0.1",
            "--processes",
            str(WORKERS),
            "--port",
            str(port),
            "--no-admin",
        ]
    return [*taskset, *command], env


def start_server(framework: str, log_path: Path) -> tuple[subprocess.Popen[bytes], str]:
    port = available_port()
    command, child_env = server_command(framework, port)
    environment = {**os.environ, **child_env, "PYTHONPATH": str(HERE)}
    if framework == "hinoki":
        environment.pop("PHASE1_DATABASE_URL", None)
        environment.pop("PHASE1_POSTGRES_CONTAINER", None)
    log_file = log_path.open("wb")
    process = subprocess.Popen(
        command,
        cwd=HERE,
        env=environment,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{framework} exited; see {log_path}")
        try:
            status, _ = request(base, "GET", "/crud-posts?page=1&page_size=1")
            if status == 200:
                return process, base
        except (OSError, TimeoutError):
            time.sleep(0.25)
    stop_server(process)
    raise TimeoutError(f"{framework} did not become ready; see {log_path}")


def stop_server(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def wait_for_low_load(timeout: int = 1200) -> tuple[float, float, float]:
    deadline = time.monotonic() + timeout
    while True:
        load = os.getloadavg()
        if load[0] <= 1.0:
            return load
        if time.monotonic() >= deadline:
            raise TimeoutError(f"one-minute load stayed above 1.0: {load}")
        print(f"Waiting for host load <= 1.0; current load: {load}", flush=True)
        time.sleep(15)


def oha(
    framework: str,
    scenario: str,
    base: str,
    seconds: int,
    output: Path | None,
) -> dict[str, Any]:
    method, path, body = scenario_request(scenario)
    command = [
        "oha",
        "-z",
        f"{seconds}s",
        "-c",
        str(CONCURRENCY),
        "--no-tui",
        "--output-format",
        "json",
        "-m",
        method,
        "-H",
        "accept-encoding: identity",
    ]
    if platform.system() == "Linux":
        command = ["taskset", "-c", LOAD_CPUS, *command]
    if body is not None:
        body_path = Path(os.environ["TMPDIR"]) / f"phase1-{scenario}.json"
        body_path.write_text(json.dumps(body))
        command.extend(["-H", "content-type: application/json", "-D", str(body_path)])
    result = execute([*command, base + path], cwd=HERE)
    data = json.loads(result.stdout)
    if output is not None:
        output.write_text(json.dumps(data, indent=2) + "\n")
    return data


def reset_sql_stats() -> None:
    with psycopg.connect(DB, autocommit=True) as connection:
        connection.execute("SELECT pg_stat_statements_reset()")


def sql_stats() -> dict[str, Any]:
    counts: dict[str, int] = {}
    statements = []
    with psycopg.connect(DB) as connection:
        rows = connection.execute(
            "SELECT query, calls FROM pg_stat_statements "
            "WHERE query NOT ILIKE '%pg_stat_statements%' ORDER BY query"
        ).fetchall()
    for query, calls in rows:
        normalized = " ".join(query.split())
        match = re.match(r"^(SELECT|INSERT|UPDATE|DELETE)\b", normalized, re.I)
        if match:
            kind = match.group(1).upper()
            counts[kind] = counts.get(kind, 0) + calls
            statements.append({"kind": kind, "calls": calls, "query": normalized})
    return {"calls_by_kind": counts, "statements": statements}


def sample(
    framework: str,
    scenario: str,
    pair: int,
    duration: int,
    warmup: int,
    output: Path,
    manifest: Any,
) -> None:
    sample_database_state()
    load = wait_for_low_load()
    started = datetime.now(UTC).isoformat()
    log_path = output / f"{framework}-{scenario}-pair-{pair}.log"
    process, base = start_server(framework, log_path)
    try:
        oha(framework, scenario, base, warmup, None)
        sample_database_state()
        reset_sql_stats()
        raw_path = output / f"{framework}-{scenario}-pair-{pair}.json"
        result = oha(framework, scenario, base, duration, raw_path)
        expected_status = "201" if scenario == "create" else "200"
        if result["summary"]["successRate"] != 1.0 or set(
            result["statusCodeDistribution"]
        ) != {expected_status}:
            raise RuntimeError(f"unexpected HTTP results in {raw_path}")
    finally:
        stop_server(process)
    statements = sql_stats()
    sample_database_state()
    manifest.write(
        json.dumps(
            {
                "framework": framework,
                "scenario": scenario,
                "pair": pair,
                "started_at_utc": started,
                "loadavg_at_sample_start": load,
                "requests_per_second": result["summary"]["requestsPerSec"],
                "success_rate": result["summary"]["successRate"],
                "requests": request_count(result),
                "sql_stats": statements,
                "raw_file": raw_path.name,
            }
        )
        + "\n"
    )
    print(
        f"{scenario} pair {pair} {framework}: "
        f"{result['summary']['requestsPerSec']:.1f} requests/s, load {load[0]:.2f}",
        flush=True,
    )


def raw_results(output: Path) -> dict[tuple[str, str, int], dict[str, Any]]:
    results: dict[tuple[str, str, int], dict[str, Any]] = {}
    for path in output.glob("*-pair-*.json"):
        framework, scenario, _, pair_text = path.stem.split("-")
        results[(framework, scenario, int(pair_text))] = json.loads(path.read_text())
    return results


def summary(output: Path) -> list[dict[str, Any]]:
    samples = raw_results(output)
    sample_metadata = {
        (item["framework"], item["scenario"], item["pair"]): item
        for item in (
            json.loads(line)
            for line in (output / "samples.jsonl").read_text().splitlines()
        )
    }
    rows = []
    for scenario in SCENARIOS:
        hinoki = [samples["hinoki", scenario, pair] for pair in range(1, 6)]
        bolt = [samples["bolt", scenario, pair] for pair in range(1, 6)]
        hinoki_calls = [
            sum(
                sample_metadata["hinoki", scenario, pair]["sql_stats"][
                    "calls_by_kind"
                ].values()
            )
            / request_count(samples["hinoki", scenario, pair])
            for pair in range(1, 6)
        ]
        bolt_calls = [
            sum(
                sample_metadata["bolt", scenario, pair]["sql_stats"][
                    "calls_by_kind"
                ].values()
            )
            / request_count(samples["bolt", scenario, pair])
            for pair in range(1, 6)
        ]
        hinoki_rps = [item["summary"]["requestsPerSec"] for item in hinoki]
        bolt_rps = [item["summary"]["requestsPerSec"] for item in bolt]
        ratios = [
            left / right for left, right in zip(hinoki_rps, bolt_rps, strict=True)
        ]
        rows.append(
            {
                "scenario": scenario,
                "hinoki_median_rps": statistics.median(hinoki_rps),
                "hinoki_min_rps": min(hinoki_rps),
                "hinoki_max_rps": max(hinoki_rps),
                "hinoki_cv_pct": statistics.stdev(hinoki_rps)
                / statistics.mean(hinoki_rps)
                * 100,
                "bolt_median_rps": statistics.median(bolt_rps),
                "bolt_min_rps": min(bolt_rps),
                "bolt_max_rps": max(bolt_rps),
                "bolt_cv_pct": statistics.stdev(bolt_rps)
                / statistics.mean(bolt_rps)
                * 100,
                "paired_ratio_median": statistics.median(ratios),
                "load_1m_start_min": min(
                    sample_metadata[framework, scenario, pair][
                        "loadavg_at_sample_start"
                    ][0]
                    for framework in ("hinoki", "bolt")
                    for pair in range(1, 6)
                ),
                "load_1m_start_max": max(
                    sample_metadata[framework, scenario, pair][
                        "loadavg_at_sample_start"
                    ][0]
                    for framework in ("hinoki", "bolt")
                    for pair in range(1, 6)
                ),
                "hinoki_sql_calls_per_request": statistics.median(hinoki_calls),
                "bolt_sql_calls_per_request": statistics.median(bolt_calls),
                "hinoki_p50_ms_median": statistics.median(
                    item["latencyPercentiles"]["p50"] for item in hinoki
                )
                * 1000,
                "bolt_p50_ms_median": statistics.median(
                    item["latencyPercentiles"]["p50"] for item in bolt
                )
                * 1000,
                "hinoki_p99_ms_median": statistics.median(
                    item["latencyPercentiles"]["p99"] for item in hinoki
                )
                * 1000,
                "bolt_p99_ms_median": statistics.median(
                    item["latencyPercentiles"]["p99"] for item in bolt
                )
                * 1000,
            }
        )
    (output / "summary.json").write_text(json.dumps(rows, indent=2) + "\n")
    return rows


def write_report(
    output: Path, rows: list[dict[str, Any]], metadata: dict[str, Any]
) -> None:
    repo = HERE.parents[1]
    raw_path = output.resolve().relative_to(repo.resolve())
    lines = [
        "---",
        "status: implemented",
        "---",
        "",
        "# Native CRUD phase 1: end-to-end orientation run",
        "",
        "This is one host-specific orientation measurement, not a performance claim or a framework ranking.",
        "The primary table reports five alternating pairs of Hinoki's automatically selected Rust generated `crud(...)` path and a manually implemented equivalent Django Bolt endpoint.",
        "Both apps use the same PostgreSQL table and deterministic 10,000-row seed. Their list, detail, create and update responses were verified against the database before timing.",
        "",
        f"Run started: {metadata['started_at_utc']}. Host: {metadata['cpu']} ({metadata['cores']} logical CPUs), {metadata['platform']}; Python {metadata['python_short']}; PostgreSQL {metadata['postgres']}; Rust {metadata['rustc']}; Oha {metadata['oha']}.",
        f"Hinoki {metadata['packages']['hinoki']} uses the Rust server and tokio-postgres; Django Bolt {metadata['packages']['django-bolt']} uses Django {metadata['packages']['django']} with psycopg {metadata['packages']['psycopg']} and its Actix server. Each starts {WORKERS} processes with a pool of {POOL} database connections per process.",
        f"Scenarios: list page sizes 20 and 100, detail, create and PATCH update. Each sample has {metadata['settings']['warmup_s']} s warm-up, then {metadata['settings']['duration_s']} s measured with {CONCURRENCY} concurrent connections. Server CPUs: {SERVER_CPUS}; load generator CPUs: {LOAD_CPUS}; response compression disabled.",
        f"Each sample's one-minute host load is checked before its server starts and must be at most 1.0. Across the samples, scenario start loads ranged from {min(row['load_1m_start_min'] for row in rows):.2f} to {max(row['load_1m_start_max'] for row in rows):.2f}. SQL calls are read from `pg_stat_statements` after each timed request window; the table reports the median calls per successful HTTP request.",
        "",
        "| Scenario | Hinoki median req/s (range, CV) | Django Bolt median req/s (range, CV) | Paired median ratio | SQL calls/request (Hinoki / Bolt) | Median p50 ms (Hinoki / Bolt) | Median p99 ms (Hinoki / Bolt) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    lines.extend(
        [
            (
                f"| {row['scenario']} | {row['hinoki_median_rps']:.1f} ({row['hinoki_min_rps']:.1f}-{row['hinoki_max_rps']:.1f}, {row['hinoki_cv_pct']:.1f}%) | "
                f"{row['bolt_median_rps']:.1f} ({row['bolt_min_rps']:.1f}-{row['bolt_max_rps']:.1f}, {row['bolt_cv_pct']:.1f}%) | "
                f"{row['paired_ratio_median']:.2f}x | "
                f"{row['hinoki_sql_calls_per_request']:.2f} / {row['bolt_sql_calls_per_request']:.2f} | "
                f"{row['hinoki_p50_ms_median']:.2f} / {row['bolt_p50_ms_median']:.2f} | "
                f"{row['hinoki_p99_ms_median']:.2f} / {row['bolt_p99_ms_median']:.2f} |"
            )
            for row in rows
        ]
    )
    lines.extend(
        [
            "",
            "The paired ratio is a descriptive ratio of the five pairwise throughput values. The samples are not a universal ranking and do not establish production performance. Host, OS, Python, server, ORM, driver, database, warm-cache state, and workload all affect these results. CV above 15% marks that scenario as unstable for interpretation; no sample is dropped.",
            "",
            phase1_results_reference(raw_path),
            "",
            "Reproduction: build the native extension in release mode, start an isolated PostgreSQL 17 container, then run `phase1_orientation.py` as documented in `benchmarks/README.md`. The script refuses to time a sample whose starting load is above 1.0.",
        ]
    )
    followup_path = output / "followup-summary.json"
    if followup_path.exists():
        followup = json.loads(followup_path.read_text())
        if "list_100" in followup:
            lines.extend(
                [
                    "",
                    "## Supplemental samples",
                    "",
                    "Because the initial `list_100` Hinoki CV exceeded 15%, five additional alternating pairs (6-10) were collected separately. This block is not pooled into the primary table:",
                    "",
                    "| Scenario | Hinoki median req/s (range, CV) | Django Bolt median req/s (range, CV) |",
                    "| --- | ---: | ---: |",
                ]
            )
            values = followup["list_100"]
            lines.append(
                f"| list_100, pairs 6-10 | {values['hinoki']['median_rps']:.1f} ({values['hinoki']['min_rps']:.1f}-{values['hinoki']['max_rps']:.1f}, {values['hinoki']['cv_pct']:.1f}%) | {values['bolt']['median_rps']:.1f} ({values['bolt']['min_rps']:.1f}-{values['bolt']['max_rps']:.1f}, {values['bolt']['cv_pct']:.1f}%) |"
            )
            lines.append(
                "The supplemental block is internally stable, while the initial Hinoki list_100 series remains unstable and shows a change in throughput regime. Treat both blocks as host-specific orientation data; the stable follow-up does not erase the initial variance."
            )
        failure_path = output / "followup-failures.json"
        if failure_path.exists():
            failure = json.loads(failure_path.read_text())
            distribution = ", ".join(
                f"{status} = {count:,}"
                for status, count in failure["http_status_distribution"].items()
            )
            lines.extend(
                [
                    "",
                    f"The supplemental update series stopped at Hinoki pair {failure['pair']} after unsuccessful responses (status distribution: {distribution}). Its server log reports database connection acquisition timeouts. The failed sample and log are retained and excluded from throughput summaries; the initial update series remains volatile (Hinoki CV 11.8%, Django Bolt CV 23.5%).",
                ]
            )
    report = "\n".join(lines) + "\n"
    (output / "orientation.md").write_text(report)
    (repo / "docs/rust-crud-orientation.md").write_text(report)


def metadata(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "started_at_utc": datetime.now(UTC).isoformat(),
        "commit": execute(
            ["git", "rev-parse", "HEAD"], cwd=HERE.parents[1]
        ).stdout.strip(),
        "machine": platform.machine(),
        "platform": platform.platform(),
        "cpu": next(
            (
                line.split(":", 1)[1].strip()
                for line in Path("/proc/cpuinfo").read_text().splitlines()
                if line.startswith("model name")
            ),
            "unknown",
        ),
        "cores": os.cpu_count(),
        "python": sys.version,
        "python_short": platform.python_version(),
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("hinoki-api", "django", "django-bolt", "psycopg", "granian")
        },
        "rustc": execute(["rustc", "--version"]).stdout.strip(),
        "loadavg_before_run": os.getloadavg(),
        "postgres": execute(
            ["docker", "exec", CONTAINER, "postgres", "--version"]
        ).stdout.strip(),
        "oha": execute(["oha", "--version"]).stdout.strip(),
        "settings": {
            "scenarios": list(SCENARIOS),
            "pairs": args.pairs,
            "duration_s": args.duration,
            "warmup_s": args.warmup,
            "concurrency": CONCURRENCY,
            "workers": WORKERS,
            "pool_per_worker": POOL,
            "server_cpus": SERVER_CPUS,
            "load_cpus": LOAD_CPUS,
            "dataset_rows": 10_000,
            "database_url": "redacted-local-disposable-container",
        },
    }


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--pairs", type=int, default=5)
    parser.add_argument("--duration", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--verify-only", action="store_true")
    return parser.parse_args()


def verify_framework(framework: str, output: Path) -> dict[str, Any]:
    process, base = start_server(framework, output / f"{framework}-verify.log")
    try:
        return assert_fixture(base)
    finally:
        stop_server(process)


def verify_pair(output: Path) -> dict[str, Any]:
    outcomes = {}
    for framework in ("hinoki", "bolt"):
        outcomes[framework] = verify_framework(framework, output)
    (output / "verification.json").write_text(json.dumps(outcomes, indent=2) + "\n")
    if outcomes["hinoki"]["list_20"] != outcomes["bolt"]["list_20"]:
        raise SystemExit("list_20 responses differ between implementations")
    if outcomes["hinoki"]["list_100"] != outcomes["bolt"]["list_100"]:
        raise SystemExit("list_100 responses differ between implementations")
    if outcomes["hinoki"]["detail"] != outcomes["bolt"]["detail"]:
        raise SystemExit("detail responses differ between implementations")
    for scenario in ("create", "update"):
        if outcomes["hinoki"][scenario] != outcomes["bolt"][scenario]:
            raise SystemExit(f"{scenario} responses differ between implementations")
    return outcomes


def verify_native_plans(output: Path) -> None:
    old_values = {
        name: os.environ.pop(name, None)
        for name in (
            "PHASE1_DATABASE_URL",
            "PHASE1_POSTGRES_CONTAINER",
            "PHASE1_DATABASE__URL",
            "PHASE1_DATABASE__MAX_SIZE",
        )
    }
    os.environ["PHASE1_DATABASE__URL"] = DB
    os.environ["PHASE1_DATABASE__MAX_SIZE"] = str(POOL)

    async def inspect_worker() -> dict[str, Any]:
        from phase1_hinoki import Phase1Posts, app

        from hinoki.runtime.kiso import KisoWorker

        worker = KisoWorker(app, "127.0.0.1", 0)
        await worker.start()
        try:
            selected = [
                selection.action
                for selection in worker.pipeline.crud_plan_selections
                if selection is not None
            ]
            armed = [
                worker.core._crud_armed(index)
                for index, selection in enumerate(worker.pipeline.crud_plan_selections)
                if selection is not None
            ]
            expected = ["list", "retrieve", "create", "update"]
            if selected != expected or not all(armed):
                routes = [
                    {
                        "method": binding.route.method,
                        "path": binding.route.path,
                        "reason": worker.pipeline.crud_plan_reasons[index],
                    }
                    for index, binding in enumerate(worker.pipeline.bindings)
                ]
                raise RuntimeError(
                    f"native CRUD plans not armed: {selected} / {armed}; {routes}"
                )
            return {
                "model": Phase1Posts.model.__name__,
                "actions": selected,
                "armed": armed,
            }
        finally:
            await worker.stop()

    try:
        status = asyncio.run(inspect_worker())
    finally:
        for name, value in old_values.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value
    (output / "native-plan-check.json").write_text(json.dumps(status, indent=2) + "\n")


def measure_pairs(args: argparse.Namespace) -> None:
    env = metadata(args)
    (args.out / "environment.json").write_text(json.dumps(env, indent=2) + "\n")
    with (args.out / "samples.jsonl").open("w") as manifest:
        for scenario in SCENARIOS:
            for pair in range(1, args.pairs + 1):
                frameworks = ("hinoki", "bolt") if pair % 2 else ("bolt", "hinoki")
                for framework in frameworks:
                    sample(
                        framework,
                        scenario,
                        pair,
                        args.duration,
                        args.warmup,
                        args.out,
                        manifest,
                    )
                    manifest.flush()
    rows = summary(args.out)
    write_report(args.out, rows, env)


def run() -> None:
    args = arguments()
    if args.pairs != 5:
        raise SystemExit("this orientation run requires exactly five paired samples")
    args.out.mkdir(parents=True, exist_ok=True)
    initialize_database()
    (args.out / "request-bodies.json").write_text(
        json.dumps({"create": CREATE_BODY, "update": UPDATE_BODY}, indent=2) + "\n"
    )
    verify_native_plans(args.out)
    verify_pair(args.out)
    if args.verify_only:
        print("Both endpoints match the same seeded database rows.")
        return
    measure_pairs(args)


if __name__ == "__main__":
    run()
