"""Run one framework's service and measure it. Everything is written to --out.

    uv run python run.py --framework hinoki --out results/2026-11-14
    uv run python run.py --framework all --smoke        # correctness only

Per framework, ``--restarts`` times (default 2): reset the database to the seed,
refuse to start if the port is already taken, start the server pinned to its CPU
set, wait for a 200, verify behaviour and query counts (verify.py), then for every
scenario warm up and take ``--runs`` samples of ``--requests`` requests each with
``bombardier`` (100 connections) and keep each raw JSON result. The server is
stopped (SIGTERM, then SIGKILL after a grace period) and the port must be free
again before the next start, so a previous server is never measured by mistake.
"""

import argparse
import contextlib
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import loadtool

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
BOMBARDIER = (
    os.environ.get("BOMBARDIER")
    or shutil.which("bombardier")
    or str(Path.home() / "go" / "bin" / "bombardier")
)

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
KISO_SERVERS = {
    "hinoki-kiso": (
        ["hinoki", "serve", "hinoki_app:app", "--server", "kiso", "--workers",
         str(WORKERS), "--host", "127.0.0.1", "--port", str(PORT)],
        {"BENCH_DATABASE__URL": DB, "BENCH_DATABASE__MAX_SIZE": str(POOL)},
    ),
}  # fmt: skip
SERVER_MODES = {
    "documented": DOCUMENTED_SERVERS,
    "same-granian": GRANIAN_SERVERS,
    "kiso": KISO_SERVERS,
}
LABELS = {
    "hinoki": "Hinoki · Granian",
    "fastapi": "FastAPI · Uvicorn",
    "ninja": "Django Ninja · Uvicorn",
    "bolt": "Django Bolt · Actix",
    "hinoki-granian": "Hinoki · Granian",
    "hinoki-kiso": "Hinoki · Kiso",
    "fastapi-granian": "FastAPI · Granian",
    "ninja-granian": "Django Ninja · Granian",
}
CONCURRENCY = 100
REQUESTS = 100_000
RESTARTS = 2
WARMUP = 10
STOP_GRACE = 15
PORT_WAIT = 10
QUIET_POLL = 10
QUIET_TIMEOUT = 1800
SCENARIOS = {
    "health": ("GET", "/health", None),
    "json-1k": ("GET", "/json-1k", None),
    "json-10k": ("GET", "/json-10k", None),
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


def http_status(url: str) -> int:
    try:
        return urllib.request.urlopen(url, timeout=2).status
    except urllib.error.HTTPError as error:
        return error.code
    except OSError:
        return 0


def wait_ready(
    process: subprocess.Popen, port: int = PORT, timeout: float = 60
) -> None:
    """Return once ``/health`` answers 200; fail at once if the server has died."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if process.poll() is not None:
            raise SystemExit(
                f"server exited before becoming ready ({process.returncode})"
            )
        if http_status(f"http://127.0.0.1:{port}/health") == 200:
            return
        time.sleep(0.3)
    raise SystemExit(f"server did not answer 200 on /health within {timeout:g} s")


def port_is_free(port: int = PORT) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def require_free_port(port: int = PORT) -> None:
    if not port_is_free(port):
        raise SystemExit(
            f"port {port} is already in use; refusing to measure a foreign or stale server"
        )


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def signal_group(pgid: int, number: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, number)


def start(name: str, log: Path) -> subprocess.Popen:
    servers = next(table for table in SERVER_MODES.values() if name in table)
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


def stop(
    process: subprocess.Popen,
    grace: float = STOP_GRACE,
    port: int | None = PORT,
    port_wait: float = PORT_WAIT,
) -> None:
    """End the whole server group: SIGTERM, wait ``grace`` s, then SIGKILL.

    A draining worker from the previous server can still answer on the shared
    port, so this returns only once the group is gone and ``port`` is free.
    """
    signal_group(process.pid, signal.SIGTERM)
    end = time.monotonic() + grace
    while group_alive(process.pid) and time.monotonic() < end:
        process.poll()
        time.sleep(0.1)
    if group_alive(process.pid):
        signal_group(process.pid, signal.SIGKILL)
    process.wait()
    while group_alive(process.pid):
        time.sleep(0.1)
    if port is not None:
        wait_port_released(port, port_wait)


def wait_port_released(port: int, timeout: float) -> None:
    end = time.monotonic() + timeout
    while not port_is_free(port):
        if time.monotonic() > end:
            raise SystemExit(
                f"a listener still holds port {port} after the server stopped"
            )
        time.sleep(0.25)


def bombardier(
    url: str,
    method: str,
    body: Path | None,
    *,
    requests: int | None = None,
    seconds: int | None = None,
    out: Path | None = None,
) -> dict:
    cmd = [
        *pin(LOAD_CPUS),
        *loadtool.command(
            BOMBARDIER,
            url,
            method,
            body,
            CONCURRENCY,
            requests=requests,
            seconds=seconds,
        ),
    ]
    text = sh(cmd, timeout=3600).stdout
    try:
        document = loadtool.parse(text)
    except ValueError as error:
        raise SystemExit(f"{url}: {error}") from None
    if out:
        out.write_text(text.rstrip("\n") + "\n")
    return document


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


def memory_bytes() -> int | None:
    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return None
    for line in meminfo.read_text().splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) * 1024
    return None


def bombardier_version() -> str:
    """The Go module version (the binary's own ``--version`` reports none)."""
    for line in command_output(["go", "version", "-m", BOMBARDIER]).splitlines():
        parts = line.split()
        if parts[:2] == ["mod", "github.com/codesenberg/bombardier"] and len(parts) > 2:
            return parts[2]
    return command_output([BOMBARDIER, "--version"]) or "unknown"


def host_details() -> dict:
    """Extra host facts that explain a run; macOS-only fields are best effort."""
    details: dict = {"pinning": "taskset" if PINNED else "none (OS scheduler)"}
    if sys.platform == "linux":
        details["memory_bytes"] = memory_bytes()
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
        "load_tool": "bombardier",
        "bombardier": bombardier_version(),
        "settings": {
            "server_mode": args.server_mode,
            "stacks": args.selected_stacks,
            "workers": WORKERS,
            "pool_per_worker": POOL,
            "concurrency": CONCURRENCY,
            "requests": args.requests,
            "restarts": args.restarts,
            "warmup_s": args.warmup,
            "max_load": args.max_load,
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
        "--server-mode", choices=tuple(SERVER_MODES), default="documented"
    )
    parser.add_argument("--out", type=Path, default=HERE / "results" / "scratch")
    parser.add_argument(
        "--runs", type=int, default=5, help="samples per scenario and start"
    )
    parser.add_argument(
        "--restarts",
        type=int,
        default=RESTARTS,
        help="server starts per framework; every sample of every start is kept",
    )
    parser.add_argument("--run-offset", type=int, default=0)
    parser.add_argument("--scenario", choices=tuple(SCENARIOS), nargs="+")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument(
        "--requests", type=int, default=REQUESTS, help="requests per measured sample"
    )
    parser.add_argument(
        "--max-load",
        type=float,
        default=None,
        help="wait before every sample until the one-minute load average is at most this",
    )
    parser.add_argument("--warmup", type=int, default=WARMUP)
    return parser.parse_args()


def selected_stacks(args: argparse.Namespace) -> list[str]:
    names = list(SERVER_MODES[args.server_mode])
    if args.framework != "all":
        if args.framework not in names:
            raise SystemExit(
                f"{args.framework!r} is not available in {args.server_mode!r} mode"
            )
        names = [args.framework]
    return names


def verify_stack(name: str, output: Path, restart: int = 1) -> None:
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
    path = output / f"{name}-verify.txt"
    text = f"# start {restart}\n{verified.stdout}{verified.stderr}"
    with path.open("w" if restart == 1 else "a") as handle:
        handle.write(text)
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


def wait_for_quiet(max_load: float | None, log: Path, label: str) -> None:
    """Hold the next sample until the one-minute load average is at most ``max_load``.

    Every sample's starting load is appended to ``log``. Giving up after
    ``QUIET_TIMEOUT`` seconds stops the run instead of measuring on a busy host.
    """
    deadline = time.monotonic() + QUIET_TIMEOUT
    load = os.getloadavg()[0]
    while max_load is not None and load > max_load:
        if time.monotonic() > deadline:
            raise SystemExit(
                f"load average stayed above {max_load} for {QUIET_TIMEOUT} s before {label}"
            )
        time.sleep(QUIET_POLL)
        load = os.getloadavg()[0]
    with log.open("a") as handle:
        handle.write(f"{label} load1={load:.2f}\n")


def measure_scenario(
    name: str,
    scenario: str,
    args: argparse.Namespace,
    body: Path,
    post: str,
    restart: int,
) -> None:
    method, path, payload = SCENARIOS[scenario]
    url = f"http://127.0.0.1:{PORT}" + path.format(post=post)
    data = body if payload else None
    bombardier(url, method, data, seconds=args.warmup)
    for run in range(1, args.runs + 1):
        sample = run + args.run_offset
        label = f"{name}-{scenario}-r{restart}-{sample}"
        wait_for_quiet(args.max_load, args.out / "loadavg.txt", label)
        bombardier(
            url, method, data, requests=args.requests, out=args.out / f"{label}.json"
        )
    if scenario == "create":
        reset_benchmark_rows()


def measure_start(
    name: str, args: argparse.Namespace, body: Path, post: str, restart: int
) -> None:
    require_free_port()
    reset_db()
    process = start(name, args.out / f"{name}-server-r{restart}.log")
    try:
        wait_ready(process)
        verify_stack(name, args.out, restart)
        if args.smoke:
            return
        for scenario in args.scenario or list(SCENARIOS):
            measure_scenario(name, scenario, args, body, post, restart)
        record_worker_cpu(process, args.out / f"{name}-workers-r{restart}.txt")
    finally:
        stop(process)


def measure_stack(name: str, args: argparse.Namespace, body: Path, post: str) -> None:
    for restart in range(1, 2 if args.smoke else args.restarts + 1):
        measure_start(name, args, body, post, restart)


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
