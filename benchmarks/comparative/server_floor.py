"""What Granian costs for a trivial response, next to Hinoki's ``/health``.

Question: how much of a served request is the server and the ASGI protocol, and how
much is Hinoki? The same ``oha`` run (64 connections) hits, with the same number of
Granian worker processes: a minimal ASGI application, a minimal RSGI application and
``hinoki_app``'s ``/health``. With one worker the server is the limit; with several
the load generator may be.

    BENCH_DATABASE__URL=postgres://... uv run python server_floor.py --workers 1 \
        --output ../results/server-floor.json
"""

import argparse
import json
import os
import platform
import shutil
import signal
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
OHA = os.environ.get("OHA", shutil.which("oha") or "oha")
URL = "http://127.0.0.1:8811/health"
DATABASE = os.environ.get(
    "PROFILE_DATABASE_URL", "postgres://postgres:bench-only@127.0.0.1:54402/bench"
)


def load(seconds: int) -> dict:
    cmd = [
        OHA,
        "-z",
        f"{seconds}s",
        "-c",
        "64",
        "--no-tui",
        "--output-format",
        "json",
        URL,
    ]
    return json.loads(
        subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    )


def measure(name: str, target: str, interface: str, workers: int, runs: int) -> dict:
    env = {**os.environ, "PYTHONPATH": str(HERE)}
    if target == "hinoki_app:app":
        env.update(BENCH_DATABASE__URL=DATABASE, BENCH_DATABASE__MAX_SIZE="10")
    server = subprocess.Popen(
        [sys.executable, "-m", "granian", "--interface", interface, "--workers",
         str(workers), "--port", "8811", "--no-log", target],
        cwd=HERE, env=env, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(URL, timeout=1).read()
                break
            except OSError:
                time.sleep(0.3)
        load(5)
        results = [load(15) for _ in range(runs)]
    finally:
        os.killpg(server.pid, signal.SIGKILL)
        server.wait()
        time.sleep(2)
    rps = [r["summary"]["requestsPerSec"] for r in results]
    return {
        "stack": name,
        "rps_median": round(statistics.median(rps)),
        "rps_runs": [round(v) for v in rps],
        "p50_ms": round(statistics.median(r["latencyPercentiles"]["p50"] * 1e3 for r in results), 3),
        "p99_ms": round(statistics.median(r["latencyPercentiles"]["p99"] * 1e3 for r in results), 3),
    }  # fmt: skip


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output")
    args = parser.parse_args()
    stacks = [
        ("granian ASGI, minimal app", "server_floor_apps:asgi", "asgi"),
        ("granian RSGI, minimal app", "server_floor_apps:rsgi", "rsgi"),
        ("hinoki /health on granian ASGI", "hinoki_app:app", "asgi"),
    ]
    report = {
        "python": platform.python_version(),
        "machine": platform.platform(),
        "loadavg_at_start": [round(v, 2) for v in os.getloadavg()],
        "workers": args.workers,
        "connections": 64,
        "runs": args.runs,
        "seconds_per_run": 15,
        "results": [measure(n, t, i, args.workers, args.runs) for n, t, i in stacks],
    }
    text = json.dumps(report, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n")
    print(text)


main()
