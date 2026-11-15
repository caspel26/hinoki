"""Run one framework's service and measure it. Everything is written to --out.

    uv run python run.py --framework hinoki --out results/2026-11-14
    uv run python run.py --framework all --smoke        # correctness only

Per framework: reset the database to the seed, start the server pinned to its CPU
set, verify behaviour and query counts (verify.py), warm up, then measure every
scenario ``--runs`` times with ``oha`` and keep each raw JSON result.
"""

import argparse
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
DB = os.environ.get(
    "BENCH_DATABASE_URL", "postgres://postgres:bench-only@127.0.0.1:54402/bench"
)
POSTGRES_CONTAINER = os.environ.get("BENCH_POSTGRES_CONTAINER", "hinoki-bench-pg")
PORT = 8801
SERVER_CPUS = "0-3"
LOAD_CPUS = "6-7"
# CPU pinning needs Linux `taskset`; elsewhere (macOS has no hard affinity) the
# processes are left to the OS scheduler and the metadata records that.
PINNED = sys.platform == "linux" and shutil.which("taskset") is not None
WORKERS = 4
POOL = 10
OHA = os.environ.get("OHA", shutil.which("oha") or "oha")

DOCUMENTED_SERVERS = {
    "hinoki": (
        ["granian", "--interface", "asgi", "--workers", str(WORKERS), "--port", str(PORT),
         "--no-log", "hinoki_app:app"],
        {"BENCH_DATABASE__URL": DB, "BENCH_DATABASE__MAX_SIZE": str(POOL)},
    ),
    "fastapi": (
        ["uvicorn", "fastapi_app:app", "--workers", str(WORKERS), "--port", str(PORT),
         "--log-level", "warning", "--no-access-log"],
        {"BENCH_DATABASE_URL": DB, "BENCH_POOL": str(POOL)},
    ),
    "ninja": (
        ["uvicorn", "ninja_app:application", "--workers", str(WORKERS), "--port", str(PORT),
         "--log-level", "warning", "--no-access-log"],
        {"BENCH_DATABASE_URL": DB, "BENCH_POOL": str(POOL)},
    ),
    "bolt": (
        ["python", "manage.py", "runbolt", "--host", "127.0.0.1", "--processes",
         str(WORKERS), "--port", str(PORT), "--no-admin"],
        {"BENCH_DATABASE_URL": DB, "BENCH_POOL": str(POOL), "BENCH_BOLT": "1",
         "DJANGO_SETTINGS_MODULE": "dj_settings"},
    ),
}  # fmt: skip
GRANIAN_SERVERS = {
    "hinoki-granian": DOCUMENTED_SERVERS["hinoki"],
    "fastapi-granian": (
        ["granian", "--interface", "asgi", "--workers", str(WORKERS), "--port", str(PORT),
         "--no-log", "fastapi_app:app"],
        {"BENCH_DATABASE_URL": DB, "BENCH_POOL": str(POOL)},
    ),
    "ninja-granian": (
        ["granian", "--interface", "asgi", "--workers", str(WORKERS), "--port", str(PORT),
         "--no-log", "ninja_app:application"],
        {"BENCH_DATABASE_URL": DB, "BENCH_POOL": str(POOL),
         "DJANGO_SETTINGS_MODULE": "dj_settings"},
    ),
}  # fmt: skip
LABELS = {
    "hinoki": "Hinoki · Granian",
    "fastapi": "FastAPI · Uvicorn",
    "ninja": "Django Ninja · Uvicorn",
    "bolt": "Django Bolt · Actix",
    "hinoki-granian": "Hinoki · Granian",
    "fastapi-granian": "FastAPI · Granian",
    "ninja-granian": "Django Ninja · Granian",
}
CONCURRENCY = 64
WARMUP = 10
DURATION = 20
SCENARIOS = {
    "health": ("GET", "/health", None),
    "list": ("GET", "/posts?page=1&page_size=20", None),
    "detail": ("GET", "/posts/{post}", None),
    "create": ("POST", "/posts", "create.json"),
}


def pin(cpus: str) -> list[str]:
    return ["taskset", "-c", cpus] if PINNED else []


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def reset_db() -> None:
    sh([sys.executable, str(HERE / "seed.py"), DB])


def wait_ready(timeout: float = 60) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2).read()
            return
        except Exception:
            time.sleep(0.3)
    raise SystemExit("server did not become ready")


def start(name: str, log: Path) -> subprocess.Popen:
    servers = DOCUMENTED_SERVERS if name in DOCUMENTED_SERVERS else GRANIAN_SERVERS
    command, env = servers[name]
    full_env = {**os.environ, "PYTHONPATH": str(HERE), **env}
    for variable in (
        "BENCH_DATABASE_URL",
        "BENCH_POOL",
        "BENCH_BOLT",
        "BENCH_POSTGRES_CONTAINER",
    ):
        if name.startswith("hinoki"):
            full_env.pop(variable, None)
    exe = (
        [sys.executable, "-m", command[0], *command[1:]]
        if command[0] != "python"
        else [sys.executable, *command[1:]]
    )
    return subprocess.Popen(
        [*pin(SERVER_CPUS), *exe],
        cwd=HERE,
        env=full_env,
        stdout=log.open("wb"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def stop(process: subprocess.Popen) -> None:
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)


def oha(
    url: str, method: str, body: Path | None, seconds: int, out: Path | None
) -> dict:
    cmd = [*pin(LOAD_CPUS), OHA, "-z", f"{seconds}s", "-c", str(CONCURRENCY),
           "--no-tui", "--output-format", "json", "-m", method]  # fmt: skip
    if body:
        cmd += ["-H", "content-type: application/json", "-D", str(body)]
    cmd.append(url)
    result = sh(cmd).stdout
    if out:
        out.write_text(result.rstrip("\n") + "\n")
    return json.loads(result)


def command_output(command: list[str]) -> str:
    try:
        return subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=60
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def cpu_model() -> str:
    if sys.platform == "darwin":
        return command_output(["sysctl", "-n", "machdep.cpu.brand_string"]) or "?"
    cpuinfo = Path("/proc/cpuinfo")
    if not cpuinfo.exists():
        return "?"
    return next(
        (
            line.split(":", 1)[1].strip()
            for line in cpuinfo.read_text().splitlines()
            if line.startswith("model name")
        ),
        "?",
    )


def host_details() -> dict:
    """Extra host facts that explain a run; macOS-only fields are best effort."""
    details: dict = {"pinning": "taskset" if PINNED else "none (OS scheduler)"}
    if sys.platform == "darwin":
        details.update(
            performance_cores=command_output(
                ["sysctl", "-n", "hw.perflevel0.physicalcpu"]
            ),
            efficiency_cores=command_output(
                ["sysctl", "-n", "hw.perflevel1.physicalcpu"]
            ),
            memory_bytes=command_output(["sysctl", "-n", "hw.memsize"]),
            os_version=command_output(["sw_vers", "-productVersion"]),
            power=command_output(["pmset", "-g", "batt"]),
            low_power_mode=command_output(["pmset", "-g"]).count(
                "lowpowermode         1"
            ),
            top_cpu_snapshot=command_output(
                ["top", "-l", "2", "-n", "8", "-o", "cpu", "-stats", "pid,command,cpu"]
            ),
            docker_vm=command_output(
                [
                    "docker",
                    "info",
                    "--format",
                    "{{.NCPU}} CPUs, {{.MemTotal}} bytes, {{.OperatingSystem}}",
                ]
            ),
        )
    return details


def environment(args: argparse.Namespace) -> dict:
    return {
        "date": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "machine": platform.platform(),
        "cpu": cpu_model(),
        "host": host_details(),
        "cores": os.cpu_count(),
        "loadavg_at_start": os.getloadavg(),
        "python": sys.version,
        "packages": sh([sys.executable, "-m", "pip", "list", "--format=json"]).stdout
        if False
        else sh(["uv", "pip", "list", "--format=json"]).stdout,
        "postgres": sh(
            ["docker", "exec", POSTGRES_CONTAINER, "postgres", "--version"]
        ).stdout.strip(),
        "oha": sh([OHA, "--version"]).stdout.strip(),
        "settings": {
            "server_mode": args.server_mode,
            "stacks": args.selected_stacks,
            "workers": WORKERS,
            "pool_per_worker": POOL,
            "concurrency": CONCURRENCY,
            "warmup_s": args.warmup,
            "duration_s": args.duration,
            "runs": args.runs,
            "run_offset": args.run_offset,
            "scenarios": args.scenario or list(SCENARIOS),
            "server_cpus": SERVER_CPUS if PINNED else None,
            "load_cpus": LOAD_CPUS if PINNED else None,
        },
    }


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--framework", default="all")
    parser.add_argument(
        "--server-mode", choices=("documented", "same-granian"), default="documented"
    )
    parser.add_argument("--out", type=Path, default=HERE / "results" / "scratch")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--run-offset", type=int, default=0)
    parser.add_argument("--scenario", choices=tuple(SCENARIOS), nargs="+")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--duration", type=int, default=DURATION)
    parser.add_argument("--warmup", type=int, default=WARMUP)
    return parser.parse_args()


def selected_stacks(args: argparse.Namespace) -> list[str]:
    names = list(
        DOCUMENTED_SERVERS if args.server_mode == "documented" else GRANIAN_SERVERS
    )
    if args.framework != "all":
        if args.framework not in names:
            raise SystemExit(
                f"{args.framework!r} is not available in {args.server_mode!r} mode"
            )
        names = [args.framework]
    return names


def verify_stack(name: str, output: Path) -> None:
    verified = subprocess.run(
        check=False,
        args=[
            sys.executable,
            str(HERE / "verify.py"),
            name,
            f"http://127.0.0.1:{PORT}",
        ],
        capture_output=True,
        text=True,
        cwd=HERE,
    )
    (output / f"{name}-verify.txt").write_text(verified.stdout + verified.stderr)
    print(verified.stdout, verified.stderr[-1500:])
    if verified.returncode != 0:
        raise SystemExit(f"{name} failed verification; no measurement taken")


def reset_benchmark_rows() -> None:
    sh(
        [
            "docker",
            "exec",
            POSTGRES_CONTAINER,
            "psql",
            "-U",
            "postgres",
            "-d",
            "bench",
            "-c",
            "DELETE FROM posts WHERE title LIKE 'bench-new%' OR title = 'verify-new'",
        ]
    )


def record_worker_cpu(process: subprocess.Popen, output: Path) -> None:
    """Keep cumulative CPU time per process of the server group.

    A stack whose work lands on one worker (SO_REUSEPORT behaves differently on
    macOS than on Linux) must not be described as a 4-process result.
    """
    rows = command_output(["ps", "-axo", "pid=,pgid=,cputime=,command="]).splitlines()
    mine = [row for row in rows if row.split(None, 3)[1:2] == [str(process.pid)]]
    output.write_text("pid pgid cputime command\n" + "\n".join(mine) + "\n")


def measure_stack(name: str, args: argparse.Namespace, body: Path, post: str) -> None:
    reset_db()
    process = start(name, args.out / f"{name}-server.log")
    try:
        wait_ready()
        verify_stack(name, args.out)
        if args.smoke:
            return
        scenarios = args.scenario or list(SCENARIOS)
        for scenario in scenarios:
            method, path, payload = SCENARIOS[scenario]
            url = f"http://127.0.0.1:{PORT}" + path.format(post=post)
            data = body if payload else None
            oha(url, method, data, args.warmup, None)
            for run in range(1, args.runs + 1):
                sample = run + args.run_offset
                oha(
                    url,
                    method,
                    data,
                    args.duration,
                    args.out / f"{name}-{scenario}-{sample}.json",
                )
            if scenario == "create":
                reset_benchmark_rows()
        record_worker_cpu(process, args.out / f"{name}-workers.txt")
    finally:
        stop(process)
        time.sleep(2)


def main() -> None:
    args = arguments()
    names = selected_stacks(args)
    args.selected_stacks = names
    args.out.mkdir(parents=True, exist_ok=True)
    if not args.smoke:
        metadata = args.out / "environment.json"
        if metadata.exists():
            stamp = time.strftime("%H%M%S")
            metadata = args.out / f"environment-{args.server_mode}-{stamp}.json"
        metadata.write_text(json.dumps(environment(args), indent=2))
    body = args.out / "create.json"
    from seed import author_id, post_id

    body.write_text(
        json.dumps(
            {
                "author_id": str(author_id(1)),
                "title": "bench-new",
                "body": "A new post.",
            }
        )
    )
    post = str(post_id(1))  # published (1 % 5 != 0)
    for name in names:
        measure_stack(name, args, body, post)


if __name__ == "__main__":
    main()
