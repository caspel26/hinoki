"""Run one framework's service and measure it. Everything is written to --out.

    uv run python run.py --framework hinoki --out results/2026-11-14
    uv run python run.py --framework all --smoke        # correctness only
    uv run python run.py --server-mode javascript --workers 8 --out results/x/js-8

Every stack serves the seven routes of Django Bolt's published comparison with
Litestar and FastAPI, over the same database (parity_seed.py), and is measured
with that comparison's method. Per stack, ``--restarts`` times (default 2): reset
the database to the seed, refuse to start if the port is already taken, start
the server pinned to its CPU set with ``--workers`` processes (default 1) and a
pool of ``--pool`` connections per process (default 8), wait for a 200, verify
behaviour, bodies and statement counts (parity_verify.py), then for every
scenario warm up with 1000 requests over 10 connections and take ``--runs``
samples (default 1) of ``--duration`` seconds (default 10; ``--requests`` for a
fixed count instead) over 100 connections with ``bombardier``, keeping each raw
JSON result. The server is stopped (SIGTERM, then SIGKILL after a grace period)
and the port must be free again before the next start, so a previous server is
never measured by mistake. Stacks are separated by a ``--cooldown`` of 20 s.
"""

import argparse
import contextlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path

import loadtool
import parity_data

HERE = Path(__file__).parent
DB = os.environ.get(
    "BENCH_DATABASE_URL", "postgres://postgres:bench-only@127.0.0.1:54402/bench"
)
POSTGRES_CONTAINER = os.environ.get("BENCH_POSTGRES_CONTAINER", "hinoki-bench-pg")
PORT = 8801
SERVER_CPUS = os.environ.get("BENCH_SERVER_CPUS", "0,1,4,5")
LOAD_CPUS = os.environ.get("BENCH_LOAD_CPUS", "2,3,6,7")
# CPU pinning needs Linux `taskset`; elsewhere (macOS has no hard affinity) the
# processes are left to the OS scheduler and the metadata records that.
PINNED = sys.platform == "linux" and shutil.which("taskset") is not None
WORKERS = 1
POOL = 8
DURATION = 10
COOLDOWN = 20
RUNS = 1
RESTARTS = 2
CONCURRENCY = 100
WARMUP = {"requests": 1000, "connections": 10}
STOP_GRACE = 15
PORT_WAIT = 10
QUIET_POLL = 10
QUIET_TIMEOUT = 1800
BOMBARDIER = (
    os.environ.get("BOMBARDIER")
    or shutil.which("bombardier")
    or str(Path.home() / "go" / "bin" / "bombardier")
)
JS_DIR = HERE / "js"
# JavaScript runtimes, resolved once: BENCH_NODE / BENCH_BUN override the PATH lookup.
RUNTIMES = {
    "node": os.environ.get("BENCH_NODE") or shutil.which("node"),
    "bun": os.environ.get("BENCH_BUN") or shutil.which("bun"),
}
JS_PACKAGES = ("elysia", "hono", "@hono/node-server", "pg")
JS_STACKS = frozenset({"elysia-bun", "hono-bun", "hono-node"})
# Django Bolt's seven routes, in its order; the auth ones carry the cookie.
SCENARIOS = {
    "json-1k": ("GET", "/json-1k"),
    "json-10k": ("GET", "/json-10k"),
    "db": ("GET", "/db"),
    "articles": ("GET", "/articles?page=1&page_size=20"),
    "article": ("GET", "/articles/1"),
    "auth-me": ("GET", "/auth/me"),
    "auth-articles": ("GET", "/auth/articles?page=1&page_size=20"),
}
AUTH_SCENARIOS = frozenset({"auth-me", "auth-articles"})


def server_modes(workers: int = WORKERS, pool: int = POOL) -> dict[str, dict]:
    """Every stack, by mode, with ``workers`` processes and ``pool`` connections each.

    Litestar, FastAPI and Ninja run on Uvicorn with uvloop and httptools, the
    ``uvicorn[standard]`` stack of Django Bolt's comparison; Bolt gets as many
    executor and ORM threads as pool connections, as there.
    """
    w, port = str(workers), str(PORT)
    hinoki = {"BENCH_DATABASE__URL": DB, "BENCH_DATABASE__MAX_SIZE": str(pool)}
    sqla = {"BENCH_DATABASE_URL": DB, "BENCH_POOL": str(pool)}
    ninja = {**sqla, "DJANGO_SETTINGS_MODULE": "parity_ninja_settings"}
    js = {**sqla, "BENCH_WORKERS": w, "BENCH_PORT": port, "NODE_ENV": "production"}
    uvicorn = ["--workers", w, "--port", port, "--loop", "uvloop", "--http",
               "httptools", "--log-level", "warning", "--no-access-log"]  # fmt: skip

    def granian(app: str) -> list[str]:
        return ["granian", "--interface", "asgi", "--workers", w, "--port", port,
                "--no-log", app]  # fmt: skip

    documented = {
        "hinoki": (granian("parity_hinoki:app"), hinoki),
        "bolt": (
            ["python", "manage.py", "runbolt", "--host", "127.0.0.1", "--processes",
             w, "--port", port, "--no-admin"],
            {**sqla, "DJANGO_SETTINGS_MODULE": "parity_dj_settings",
             "DJANGO_BOLT_EXECUTOR_THREADS": str(pool),
             "DJANGO_BOLT_ORM_THREADS": str(pool)},
        ),
        "litestar": (["uvicorn", "parity_litestar:app", *uvicorn], sqla),
        "fastapi": (["uvicorn", "parity_fastapi:app", *uvicorn], sqla),
        "ninja": (["uvicorn", "parity_ninja:application", *uvicorn], ninja),
    }  # fmt: skip
    return {
        "documented": documented,
        "same-granian": {
            "hinoki-granian": documented["hinoki"],
            "litestar-granian": (granian("parity_litestar:app"), sqla),
            "fastapi-granian": (granian("parity_fastapi:app"), sqla),
            "ninja-granian": (granian("parity_ninja:application"), ninja),
        },
        "kiso": {
            "hinoki-kiso": (
                ["hinoki", "serve", "parity_hinoki:app", "--server", "kiso",
                 "--workers", w, "--host", "127.0.0.1", "--port", port],
                hinoki,
            ),
        },
        # The three JavaScript servers of Django Bolt's comparison, in its order.
        "javascript": {
            "elysia-bun": (["bun", "js/elysia_app.js"], js),
            "hono-bun": (["bun", "js/hono_bun.js"], js),
            "hono-node": (["node", "js/hono_node.js"], js),
        },
    }  # fmt: skip


SERVER_MODES = server_modes()
LABELS = {
    "hinoki": "Hinoki · Granian",
    "bolt": "Django Bolt · Actix",
    "litestar": "Litestar · Uvicorn",
    "fastapi": "FastAPI · Uvicorn",
    "ninja": "Django Ninja · Uvicorn",
    "hinoki-granian": "Hinoki · Granian",
    "litestar-granian": "Litestar · Granian",
    "fastapi-granian": "FastAPI · Granian",
    "ninja-granian": "Django Ninja · Granian",
    "hinoki-kiso": "Hinoki · Kiso",
    "elysia-bun": "Elysia · Bun",
    "hono-bun": "Hono · Bun",
    "hono-node": "Hono · Node.js",
}


def scenario_headers(scenario: str) -> tuple[str, ...]:
    """A freshly minted session cookie for the authenticated scenarios."""
    if scenario not in AUTH_SCENARIOS:
        return ()
    return (f"cookie: {parity_data.COOKIE}={parity_data.mint_token()}",)


def pin(cpus: str) -> list[str]:
    return ["taskset", "-c", cpus] if PINNED else []


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def reset_db() -> None:
    sh([sys.executable, str(HERE / "parity_seed.py"), DB])


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


def server_table(name: str) -> tuple[list[str], dict[str, str]]:
    servers = next(table for table in SERVER_MODES.values() if name in table)
    return servers[name]


def server_env(
    name: str, base: Mapping[str, str], out: Path | None = None
) -> dict[str, str]:
    """``base`` with only the ``BENCH_*`` variables the stack declares.

    A variable left over from the caller's shell (another stack's settings, the
    runner's own options) must not change how a server configures itself. The
    JavaScript stacks also get the path of the two ``json-*`` values, written
    to ``out`` from parity_data.py.
    """
    _, env = server_table(name)
    inherited = {k: v for k, v in base.items() if not k.startswith("BENCH_")}
    extra = {}
    if name in JS_STACKS and out is not None:
        extra["BENCH_PAYLOADS"] = str(write_payloads(out))
    return {**inherited, "PYTHONPATH": str(HERE), **env, **extra}


def write_payloads(out: Path) -> Path:
    path = (out / "payloads.json").resolve()
    path.write_text(json.dumps(parity_data.PAYLOADS, separators=(",", ":")))
    return path


def server_command(command: list[str]) -> list[str]:
    """Python tools run as modules of this interpreter; JavaScript runtimes by path."""
    if command[0] in RUNTIMES:
        runtime = RUNTIMES[command[0]]
        if runtime is None:
            raise SystemExit(
                f"{command[0]} is not installed; see benchmarks/comparative/README.md"
            )
        return [runtime, *command[1:]]
    if command[0] == "python":
        return [sys.executable, *command[1:]]
    return [sys.executable, "-m", *command]


def start(name: str, log: Path) -> subprocess.Popen:
    command, _ = server_table(name)
    return subprocess.Popen(
        [*pin(SERVER_CPUS), *server_command(command)],
        cwd=HERE,
        env=server_env(name, os.environ, log.parent),
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
    *,
    requests: int | None = None,
    seconds: int | None = None,
    out: Path | None = None,
    connections: int = CONCURRENCY,
    headers: tuple[str, ...] = (),
) -> dict:
    cmd = [
        *pin(LOAD_CPUS),
        *loadtool.command(
            BOMBARDIER,
            url,
            "GET",
            None,
            connections,
            requests=requests,
            seconds=seconds,
            headers=headers,
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


def installed_version(js_dir: Path, package: str) -> str | None:
    manifest = js_dir / "node_modules" / package / "package.json"
    if not manifest.exists():
        return None
    return str(json.loads(manifest.read_text())["version"])


def runtime_version(path: str | None) -> str | None:
    found = re.search(
        r"\d+\.\d+\.\d+", command_output([path, "--version"]) if path else ""
    )
    return found[0] if found else None


def javascript_versions(
    js_dir: Path = JS_DIR, runtimes: Mapping[str, str | None] = RUNTIMES
) -> dict:
    """Runtime versions and the package versions `npm ci` actually installed."""
    return {
        "runtimes": {name: runtime_version(path) for name, path in runtimes.items()},
        "packages": {name: installed_version(js_dir, name) for name in JS_PACKAGES},
    }


def environment(args: argparse.Namespace) -> dict:
    javascript = (
        {"javascript": javascript_versions()}
        if args.server_mode == "javascript"
        else {}
    )
    return {
        **javascript,
        "date": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "machine": platform.platform(),
        "cpu": cpu_model(),
        "host": host_details(),
        "cores": os.cpu_count(),
        "loadavg_at_start": os.getloadavg(),
        "python": sys.version,
        "packages": sh(["uv", "pip", "list", "--format=json"]).stdout,
        "postgres": sh(
            ["docker", "exec", POSTGRES_CONTAINER, "postgres", "--version"]
        ).stdout.strip(),
        "load_tool": "bombardier",
        "bombardier": bombardier_version(),
        "settings": {
            "server_mode": args.server_mode,
            "stacks": args.selected_stacks,
            "workers": args.workers,
            "pool_per_worker": args.pool,
            "concurrency": CONCURRENCY,
            **sample_stop(args),
            "restarts": args.restarts,
            "warmup": dict(WARMUP),
            "cooldown_s": args.cooldown,
            "max_load": args.max_load,
            "runs": args.runs,
            "run_offset": args.run_offset,
            "scenarios": args.scenario or list(SCENARIOS),
            "server_cpus": SERVER_CPUS if PINNED else None,
            "load_cpus": LOAD_CPUS if PINNED else None,
        },
    }


def sample_stop(args: argparse.Namespace) -> dict[str, int]:
    """What ends one sample: ``--requests`` when given, else ``--duration``."""
    if args.requests:
        return {"requests": args.requests}
    return {"duration_s": args.duration}


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--framework", default="all")
    parser.add_argument(
        "--server-mode", choices=tuple(SERVER_MODES), default="documented"
    )
    parser.add_argument("--out", type=Path, default=HERE / "results" / "scratch")
    parser.add_argument(
        "--runs", type=int, default=RUNS, help="samples per scenario and start"
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
        "--duration", type=int, default=DURATION, help="seconds per measured sample"
    )
    parser.add_argument(
        "--requests",
        type=int,
        default=None,
        help="requests per measured sample instead of --duration",
    )
    parser.add_argument(
        "--workers", type=int, default=WORKERS, help="server processes per stack"
    )
    parser.add_argument(
        "--pool", type=int, default=POOL, help="database connections per process"
    )
    parser.add_argument(
        "--cooldown", type=int, default=COOLDOWN, help="seconds of rest between stacks"
    )
    parser.add_argument(
        "--max-load",
        type=float,
        default=None,
        help="wait before every sample until the one-minute load average is at most this",
    )
    args = parser.parse_args(argv)
    SERVER_MODES.update(server_modes(args.workers, args.pool))
    return args


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
            str(HERE / "parity_verify.py"),
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


def record_worker_cpu(process: subprocess.Popen, output: Path) -> None:
    """Keep cumulative CPU time per process of the server group.

    A stack whose work lands on one worker (SO_REUSEPORT behaves differently on
    macOS than on Linux) must not be described as a multi-process result.
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
    name: str, scenario: str, args: argparse.Namespace, restart: int
) -> None:
    _, path = SCENARIOS[scenario]
    url = f"http://127.0.0.1:{PORT}{path}"
    headers = scenario_headers(scenario)
    stop_rule = (
        {"requests": args.requests} if args.requests else {"seconds": args.duration}
    )
    bombardier(url, **WARMUP, headers=headers)
    for run in range(1, args.runs + 1):
        sample = run + args.run_offset
        label = f"{name}-{scenario}-r{restart}-{sample}"
        wait_for_quiet(args.max_load, args.out / "loadavg.txt", label)
        bombardier(url, **stop_rule, out=args.out / f"{label}.json", headers=headers)


def measure_start(name: str, args: argparse.Namespace, restart: int) -> None:
    require_free_port()
    reset_db()
    process = start(name, args.out / f"{name}-server-r{restart}.log")
    try:
        wait_ready(process)
        verify_stack(name, args.out, restart)
        if args.smoke:
            return
        for scenario in args.scenario or list(SCENARIOS):
            measure_scenario(name, scenario, args, restart)
        record_worker_cpu(process, args.out / f"{name}-workers-r{restart}.txt")
    finally:
        stop(process)


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
    for index, name in enumerate(names):
        if index and args.cooldown and not args.smoke:
            time.sleep(args.cooldown)
        for restart in range(1, 2 if args.smoke else args.restarts + 1):
            measure_start(name, args, restart)


if __name__ == "__main__":
    main()
