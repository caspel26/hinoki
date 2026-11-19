"""Baseline for the Rust request path with handwritten handlers (phase 2, step 2.0).

Measures the same fixture app (``phase2_hinoki``) on Hinoki's Rust server and on
Granian, one scenario at a time, alternating the two stacks for five pairs. The
handlers do no work, so each result is the request path around them. A response
check precedes measuring: both stacks must return the same status and body for
every scenario. The lane each route takes on the Rust server is recorded too.

    uv run --no-sync python phase2_baseline.py --out results/YYYY-MM-DD/request-path
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import platform
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from phase1_metrics import request_count
from phase1_orientation import (
    available_port,
    execute,
    stop_server,
    wait_for_low_load,
)

HERE = Path(__file__).parent
DB = os.environ.get("P2BENCH_DATABASE_URL", "")
# "handlers" measures the JSON request path; "sources" the text sources and
# renderers; "sync" the JSON request path with sync (``def``) handlers.
SUITE = os.environ.get("PHASE2_SUITE", "handlers")
APP_MODULES = {
    "handlers": "phase2_hinoki",
    "sources": "phase2_sources_hinoki",
    "sync": "phase2_sync_hinoki",
}
APP_MODULE = APP_MODULES[SUITE]
WORKERS = 4
POOL = 10
CONCURRENCY = 64
SERVER_CPUS = "0-3"
LOAD_CPUS = "6-7"
STACKS = ("rust", "granian")
SMALL = {"title": "Baseline", "body": "x" * 64, "count": 3}
BIG_10K = {"title": "Baseline", "body": "x" * 10_000, "count": 3}
BIG_100K = {"title": "Baseline", "body": "x" * 100_000, "count": 3}
FORM = ("application/x-www-form-urlencoded", b"title=Baseline&count=3&flag=true")
XML = (
    "application/xml",
    b"<item><title>Baseline</title><count>3</count><flag>true</flag></item>",
)
# name -> (method, path, body, expected status); a body is a JSON value, a
# (content type, bytes) pair, or None
Body = dict[str, Any] | tuple[str, bytes] | None
HANDLER_SCENARIOS: dict[str, tuple[str, str, Body, int]] = {
    "health": ("GET", "/health", None, 200),
    "echo_str": ("GET", "/echo-str/abc?view=full", None, 200),
    "echo_typed": ("GET", "/echo/42?limit=7&ratio=2.5&flag=true", None, 200),
    "post_small": ("POST", "/items", SMALL, 201),
    "post_10k": ("POST", "/items", BIG_10K, 201),
    "post_100k": ("POST", "/items", BIG_100K, 201),
    "patch": ("PATCH", "/items/42", {"title": "Changed", "count": 5}, 200),
    "delete": ("DELETE", "/items/42", None, 204),
    "post_tx": ("POST", "/items-tx", SMALL, 201),
}
SOURCE_SCENARIOS: dict[str, tuple[str, str, Body, int]] = {
    "health": ("GET", "/health", None, 200),
    "form": ("POST", "/form", FORM, 201),
    "xml": ("POST", "/xml", XML, 201),
    "query": ("GET", "/query?term=baseline&limit=7&flag=true", None, 200),
    "html": ("GET", "/render/html", None, 200),
    "xml_render": ("GET", "/render/xml", None, 200),
    "request": ("GET", "/request-param", None, 200),
}
SYNC_SCENARIOS = {
    name: scenario for name, scenario in HANDLER_SCENARIOS.items() if name != "post_tx"
}
SCENARIOS = {
    "handlers": HANDLER_SCENARIOS,
    "sources": SOURCE_SCENARIOS,
    "sync": SYNC_SCENARIOS,
}[SUITE]


def encoded(body: Body) -> tuple[bytes, str] | None:
    if body is None:
        return None
    if isinstance(body, tuple):
        return body[1], body[0]
    return json.dumps(body).encode(), "application/json"


def server_command(stack: str, port: int) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "hinoki",
        "serve",
        f"{APP_MODULE}:app",
        "--server",
        stack,
        "--workers",
        str(WORKERS),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    taskset = ["taskset", "-c", SERVER_CPUS] if platform.system() == "Linux" else []
    return [*taskset, *command]


def call(
    base: str, scenario: str, timeout: float = 5.0
) -> tuple[int, bytes, dict[str, str]]:
    method, path, body, _ = SCENARIOS[scenario]
    payload = encoded(body)
    data = payload[0] if payload else None
    headers = {"accept-encoding": "identity"}
    if payload:
        headers["content-type"] = payload[1]
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def start_server(stack: str, log_path: Path) -> tuple[subprocess.Popen[bytes], str]:
    port = available_port()
    environment = {
        **os.environ,
        "PYTHONPATH": str(HERE),
        "PHASE2_DATABASE__URL": DB,
        "PHASE2_DATABASE__MAX_SIZE": str(POOL),
    }
    log_file = log_path.open("wb")
    process = subprocess.Popen(
        server_command(stack, port),
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
            raise RuntimeError(f"{stack} exited; see {log_path}")
        try:
            if call(base, "health")[0] == 200:
                return process, base
        except (OSError, TimeoutError):
            time.sleep(0.25)
    stop_server(process)
    raise TimeoutError(f"{stack} did not become ready; see {log_path}")


def oha(scenario: str, base: str, seconds: int, output: Path | None) -> dict[str, Any]:
    method, path, body, _ = SCENARIOS[scenario]
    command = [
        "oha", "-z", f"{seconds}s", "-c", str(CONCURRENCY), "--no-tui",
        "--output-format", "json", "-m", method, "-H", "accept-encoding: identity",
    ]  # fmt: skip
    if platform.system() == "Linux":
        command = ["taskset", "-c", LOAD_CPUS, *command]
    payload = encoded(body)
    if payload:
        body_path = Path(os.environ["TMPDIR"]) / f"phase2-{scenario}.body"
        body_path.write_bytes(payload[0])
        command.extend(["-H", f"content-type: {payload[1]}", "-D", str(body_path)])
    data = json.loads(execute([*command, base + path], cwd=HERE).stdout)
    if output is not None:
        output.write_text(json.dumps(data, indent=2) + "\n")
    return data


def comparable(headers: dict[str, str]) -> dict[str, str]:
    skipped = {"date", "x-request-id", "server"}
    return {k.lower(): v for k, v in headers.items() if k.lower() not in skipped}


def verify(output: Path) -> dict[str, Any]:
    seen: dict[str, dict[str, Any]] = {}
    for stack in STACKS:
        process, base = start_server(stack, output / f"{stack}-verify.log")
        try:
            for scenario, (_, _, _, expected) in SCENARIOS.items():
                status, body, headers = call(base, scenario)
                if status != expected:
                    raise SystemExit(f"{stack} {scenario}: HTTP {status} != {expected}")
                seen.setdefault(scenario, {})[stack] = {
                    "status": status,
                    "body": body.decode()[:200],
                    "headers": comparable(headers),
                }
        finally:
            stop_server(process)
    for scenario, by_stack in seen.items():
        rust, granian = by_stack["rust"], by_stack["granian"]
        if (rust["status"], rust["body"]) != (granian["status"], granian["body"]):
            raise SystemExit(f"{scenario}: the two stacks answered differently")
    (output / "verification.json").write_text(json.dumps(seen, indent=2) + "\n")
    return seen


def lanes(output: Path) -> dict[str, str]:
    """Which lane each route takes on the Rust server (selection at startup)."""
    os.environ["PHASE2_DATABASE__URL"] = DB

    async def inspect_worker() -> dict[str, str]:
        app = importlib.import_module(APP_MODULE).app

        from hinoki.runtime.rustserver import RustWorker
        from hinoki.runtime.servercore import native_handler_eligible

        worker = RustWorker(app, "127.0.0.1", 0)
        await worker.start()
        try:
            return {
                f"{b.route.method} {b.route.path}": (
                    "native handler lane" if native_handler_eligible(b) else "python"
                )
                for b in worker.pipeline.bindings
            }
        finally:
            await worker.stop()

    found = asyncio.run(inspect_worker())
    (output / "lanes.json").write_text(json.dumps(found, indent=2) + "\n")
    return found


def sample(
    stack: str, scenario: str, pair: int, args: argparse.Namespace, manifest: Any
) -> None:
    load = wait_for_low_load(args.load_timeout)
    started = datetime.now(UTC).isoformat()
    name = f"{stack}-{scenario}-pair-{pair}"
    process, base = start_server(stack, args.out / f"{name}.log")
    try:
        oha(scenario, base, args.warmup, None)
        result = oha(scenario, base, args.duration, args.out / f"{name}.json")
    finally:
        stop_server(process)
    expected = str(SCENARIOS[scenario][3])
    if result["summary"]["successRate"] != 1.0 or set(
        result["statusCodeDistribution"]
    ) != {expected}:
        raise RuntimeError(f"unexpected HTTP results in {name}.json")
    manifest.write(
        json.dumps(
            {
                "stack": stack,
                "scenario": scenario,
                "pair": pair,
                "started_at_utc": started,
                "loadavg_at_sample_start": load,
                "requests_per_second": result["summary"]["requestsPerSec"],
                "requests": request_count(result),
                "raw_file": f"{name}.json",
            }
        )
        + "\n"
    )
    manifest.flush()
    print(
        f"{scenario} pair {pair} {stack}: "
        f"{result['summary']['requestsPerSec']:.1f} req/s, load {load[0]:.2f}",
        flush=True,
    )


def summarize(output: Path) -> list[dict[str, Any]]:
    rows = [json.loads(x) for x in (output / "samples.jsonl").read_text().splitlines()]
    summary = []
    for scenario in SCENARIOS:
        for stack in STACKS:
            rps = [
                r["requests_per_second"]
                for r in rows
                if r["scenario"] == scenario and r["stack"] == stack
            ]
            if not rps:
                continue
            mean = statistics.fmean(rps)
            cv = statistics.stdev(rps) / mean * 100 if len(rps) > 1 else 0.0
            summary.append(
                {
                    "scenario": scenario,
                    "stack": stack,
                    "samples": len(rps),
                    "median_rps": round(statistics.median(rps), 1),
                    "min_rps": round(min(rps), 1),
                    "max_rps": round(max(rps), 1),
                    "cv_percent": round(cv, 1),
                    "unstable": cv > 15,
                }
            )
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def metadata(args: argparse.Namespace) -> dict[str, Any]:
    cpu = next(
        (
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        ),
        "unknown",
    )
    return {
        "started_at_utc": datetime.now(UTC).isoformat(),
        "commit": execute(
            ["git", "rev-parse", "HEAD"], cwd=HERE.parents[1]
        ).stdout.strip(),
        "platform": platform.platform(),
        "cpu": cpu,
        "cores": os.cpu_count(),
        "python": platform.python_version(),
        "packages": {n: importlib.metadata.version(n) for n in ("hinoki", "granian")},
        "rustc": execute(["rustc", "--version"]).stdout.strip(),
        "oha": execute(["oha", "--version"]).stdout.strip(),
        "loadavg_before_run": os.getloadavg(),
        "settings": {
            "suite": SUITE,
            "scenarios": list(SCENARIOS),
            "stacks": list(STACKS),
            "pairs": args.pairs,
            "duration_s": args.duration,
            "warmup_s": args.warmup,
            "concurrency": CONCURRENCY,
            "workers": WORKERS,
            "pool_per_worker": POOL,
            "server_cpus": SERVER_CPUS,
            "load_cpus": LOAD_CPUS,
            "database_url": "redacted-local-disposable-container",
        },
    }


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--pairs", type=int, default=5)
    parser.add_argument("--duration", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--load-timeout", type=int, default=6 * 3600)
    parser.add_argument("--scenario", action="append", choices=list(SCENARIOS))
    parser.add_argument("--verify-only", action="store_true")
    return parser.parse_args()


def run() -> None:
    args = arguments()
    if SUITE == "handlers" and not DB:
        raise SystemExit("set P2BENCH_DATABASE_URL to a disposable PostgreSQL URL")
    args.out.mkdir(parents=True, exist_ok=True)
    verify(args.out)
    found = lanes(args.out)
    print(json.dumps(found, indent=2))
    if args.verify_only:
        return
    (args.out / "environment.json").write_text(
        json.dumps(metadata(args), indent=2) + "\n"
    )
    with (args.out / "samples.jsonl").open("a") as manifest:
        for scenario in args.scenario or list(SCENARIOS):
            for pair in range(1, args.pairs + 1):
                order = STACKS if pair % 2 else tuple(reversed(STACKS))
                for stack in order:
                    sample(stack, scenario, pair, args, manifest)
    for row in summarize(args.out):
        print(row)


if __name__ == "__main__":
    run()
