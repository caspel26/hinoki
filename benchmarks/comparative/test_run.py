import importlib
import json
import logging
import signal
import socket
import subprocess
import sys
from pathlib import Path

import jwt
import loadtool  # type: ignore[import-not-found]
import parity_data  # type: ignore[import-not-found]
import payloads  # type: ignore[import-not-found]
import pytest
import run  # type: ignore[import-not-found]

BOMBARDIER_OUTPUT = json.dumps(
    {
        "spec": {"numberOfConnections": 100, "numberOfRequests": 100000},
        "result": {
            "timeTakenSeconds": 4.0,
            "req1xx": 0,
            "req2xx": 99990,
            "req3xx": 0,
            "req4xx": 4,
            "req5xx": 0,
            "others": 6,
            "latency": {
                "mean": 900.0,
                "percentiles": {"50": 800, "75": 900, "95": 1500, "99": 2500},
            },
            "rps": {"mean": 1.0},
        },
    }
)


def test_command_uses_fixed_requests_connections_and_json_percentiles() -> None:
    command = loadtool.command(
        "bombardier", "http://h/x", "POST", Path("body.json"), 100, requests=100000
    )

    assert command[:1] == ["bombardier"]
    assert command[command.index("-c") : command.index("-c") + 2] == ["-c", "100"]
    assert command[command.index("-n") : command.index("-n") + 2] == ["-n", "100000"]
    assert "-d" not in command
    assert {"-l", "json"} <= set(command)
    assert command[command.index("-m") : command.index("-m") + 2] == ["-m", "POST"]
    assert command[command.index("-f") : command.index("-f") + 2] == ["-f", "body.json"]
    assert "content-type: application/json" in command
    assert command[-1] == "http://h/x"


def test_warmup_command_is_timed_and_has_no_body_without_one() -> None:
    command = loadtool.command("bombardier", "http://h/x", "GET", None, 100, seconds=10)

    assert command[command.index("-d") : command.index("-d") + 2] == ["-d", "10s"]
    assert "-n" not in command
    assert "-f" not in command


def test_command_needs_exactly_one_stopping_rule() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        loadtool.command("bombardier", "http://h/x", "GET", None, 100)


def test_command_sends_each_extra_header() -> None:
    command = loadtool.command(
        "bombardier", "http://h/x", "GET", None, 10, requests=1000,
        headers=("cookie: access_token=t", "x-a: 1"),
    )  # fmt: skip

    assert command[command.index("-c") + 1] == "10"
    assert [command[i + 1] for i, part in enumerate(command) if part == "-H"] == [
        "cookie: access_token=t",
        "x-a: 1",
    ]
    assert command[-1] == "http://h/x"


def test_every_mode_is_sized_from_workers_and_pool() -> None:
    modes = run.server_modes(workers=8, pool=4, runtime_threads=3)

    assert {name for table in modes.values() for name in table} == set(run.LABELS)
    for table in modes.values():
        for name, (command, env) in table.items():
            if name in run.JS_STACKS:
                assert env["BENCH_WORKERS"] == "8", name
            else:
                flag = "--processes" if name == "bolt" else "--workers"
                assert command[command.index(flag) + 1] == "8", name
            assert "4" in {env.get("BENCH_POOL"), env.get("BENCH_DATABASE__MAX_SIZE")}
    bolt_env = modes["documented"]["bolt"][1]
    assert bolt_env["DJANGO_BOLT_EXECUTOR_THREADS"] == "4"
    assert bolt_env["DJANGO_BOLT_ORM_THREADS"] == "4"
    assert modes["kiso"]["hinoki-kiso"][1]["BENCH_SERVER__RUNTIME_THREADS"] == "3"
    assert "BENCH_SERVER__RUNTIME_THREADS" not in modes["documented"]["hinoki"][1]
    assert all(
        "BENCH_SERVER__RUNTIME_THREADS" not in env
        for name, (_, env) in modes["same-granian"].items()
        if name != "hinoki-kiso"
    )
    for name in ("litestar", "fastapi", "ninja"):
        command = modes["documented"][name][0]
        assert command[0] == "uvicorn"
        assert command[command.index("--http") + 1] == "httptools"
        assert command[command.index("--loop") + 1] == "uvloop"


def test_defaults_follow_django_bolts_method_and_workers_is_a_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run, "SERVER_MODES", dict(run.SERVER_MODES))
    args = run.arguments([])

    assert (
        args.workers,
        args.pool,
        args.runtime_threads,
        args.duration,
        args.requests,
    ) == (
        1,
        8,
        1,
        10,
        None,
    )
    assert (args.restarts, args.runs, args.cooldown) == (2, 1, 20)
    assert run.WARMUP == {"requests": 1000, "connections": 10}
    assert run.CONCURRENCY == 100
    assert run.sample_stop(args) == {"duration_s": 10}

    args = run.arguments(["--workers", "8", "--requests", "100000"])
    command, _ = run.SERVER_MODES["documented"]["hinoki"]
    assert command[command.index("--workers") + 1] == "8"
    kiso_args = run.arguments(
        [
            "--server-mode",
            "kiso",
            "--runtime-threads",
            "3",
        ]
    )
    assert kiso_args.runtime_threads == 3
    assert (
        run.SERVER_MODES["kiso"]["hinoki-kiso"][1]["BENCH_SERVER__RUNTIME_THREADS"]
        == "3"
    )
    assert run.sample_stop(args) == {"requests": 100000}


def test_runtime_threads_must_be_positive(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        run.arguments(["--server-mode", "kiso", "--runtime-threads", "0"])
    assert "--runtime-threads must be at least 1" in capsys.readouterr().err


def test_runtime_threads_are_kiso_only(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        run.arguments(["--runtime-threads", "2"])
    assert "only available with --server-mode kiso" in capsys.readouterr().err


def test_git_source_state_records_commit_and_dirty_status(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "--quiet")
    (tmp_path / "source.py").write_text("value = 1\n")
    git("add", "source.py")
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Benchmark test",
            "-c",
            "user.email=benchmark@example.test",
            "commit",
            "--quiet",
            "-m",
            "baseline",
        ],
        check=True,
    )
    commit = git("rev-parse", "HEAD")

    assert run.git_source_state(tmp_path) == {"git_hash": commit, "dirty": False}

    (tmp_path / "source.py").write_text("value = 2\n")
    assert run.git_source_state(tmp_path) == {"git_hash": commit, "dirty": True}


def test_only_the_auth_scenarios_carry_a_valid_session_cookie() -> None:
    (header,) = run.scenario_headers("auth-articles")
    name, _, value = header.partition(": ")[2].partition("=")
    claims = jwt.decode(value, parity_data.SECRET, algorithms=[parity_data.ALGORITHM])

    assert name == parity_data.COOKIE
    assert claims["sub"] == str(parity_data.AUTH_USER_ID)
    assert claims["exp"] > claims["iat"]
    assert run.scenario_headers("articles") == ()


def test_parse_summarizes_rate_latency_in_milliseconds_and_unexpected() -> None:
    summary = loadtool.summarize(loadtool.parse(BOMBARDIER_OUTPUT))

    assert summary["completed"] == 100000
    assert summary["rps"] == pytest.approx(25000)
    assert summary["latency_ms"] == {"p50": 0.8, "p95": 1.5, "p99": 2.5}
    assert summary["wrong"] == 10


def test_parse_rejects_output_that_is_not_a_bombardier_result() -> None:
    for text in ("", "not json", "{}", '{"result": {}}'):
        with pytest.raises(ValueError, match="bombardier"):
            loadtool.parse(text)


def listening_socket() -> tuple[socket.socket, int]:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    return server, server.getsockname()[1]


def test_port_guard_sees_a_foreign_listener_and_a_free_port() -> None:
    server, port = listening_socket()
    assert not run.port_is_free(port)
    with pytest.raises(SystemExit, match="already in use"):
        run.require_free_port(port)
    server.close()
    assert run.port_is_free(port)
    run.require_free_port(port)


def sleeper(ignore_term: bool) -> subprocess.Popen:
    ignore = "signal.signal(signal.SIGTERM, signal.SIG_IGN);" if ignore_term else ""
    code = f"import signal,time;{ignore}print('up',flush=True);time.sleep(60)"
    process = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, start_new_session=True
    )
    assert process.stdout is not None
    process.stdout.readline()
    return process


def cpu_numbers(spec: str) -> set[int]:
    numbers: set[int] = set()
    for part in spec.split(","):
        low, _, high = part.partition("-")
        numbers.update(range(int(low), int(high or low) + 1))
    return numbers


@pytest.mark.skipif(sys.platform != "linux", reason="needs Linux CPU topology")
def test_server_and_load_generator_never_share_a_physical_core() -> None:
    def core(cpu: int) -> str:
        path = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list")
        if not path.exists():
            pytest.skip(f"cpu{cpu} is not present on this host")
        return path.read_text().strip()

    server = {core(cpu) for cpu in cpu_numbers(run.SERVER_CPUS)}
    load = {core(cpu) for cpu in cpu_numbers(run.LOAD_CPUS)}

    assert server.isdisjoint(load)


def test_hinoki_servers_do_not_inherit_the_benchmark_cpu_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Hinoki reads BENCH_* as settings and rejects names it does not know.
    monkeypatch.setenv("BENCH_SERVER_CPUS", "0,1")
    monkeypatch.setenv("BENCH_LOAD_CPUS", "2,3")
    seen: dict[str, str] = {}

    def fake_popen(*_args: object, env: dict[str, str], **_kwargs: object) -> object:
        seen.update(env)
        return object()

    monkeypatch.setattr(run.subprocess, "Popen", fake_popen)
    run.start("hinoki", tmp_path / "server.log")

    assert "BENCH_SERVER_CPUS" not in seen
    assert "BENCH_LOAD_CPUS" not in seen


def test_stop_ends_the_whole_group_on_sigterm() -> None:
    process = sleeper(ignore_term=False)

    run.stop(process, grace=5, port=None)

    assert process.returncode == -signal.SIGTERM
    assert not run.group_alive(process.pid)


def test_stop_escalates_to_sigkill_after_the_grace_period() -> None:
    process = sleeper(ignore_term=True)

    run.stop(process, grace=0.5, port=None)

    assert process.returncode == -signal.SIGKILL
    assert not run.group_alive(process.pid)


def test_stop_refuses_to_continue_while_something_holds_the_port() -> None:
    server, port = listening_socket()
    process = sleeper(ignore_term=False)

    with pytest.raises(SystemExit, match="still holds port"):
        run.stop(process, grace=5, port=port, port_wait=0.5)
    server.close()


def test_readiness_fails_fast_when_the_server_process_has_exited() -> None:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()

    with pytest.raises(SystemExit, match="exited before becoming ready"):
        run.wait_ready(process, port=1, timeout=5)


def test_readiness_requires_a_200_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    process = sleeper(ignore_term=False)
    monkeypatch.setattr(run, "http_status", lambda _url: 503)

    with pytest.raises(SystemExit, match="did not answer 200"):
        run.wait_ready(process, port=1, timeout=0.5)
    run.stop(process, grace=5, port=None)


def test_scenarios_are_django_bolts_seven_get_routes_in_its_order() -> None:
    assert list(run.SCENARIOS) == [
        "json-1k", "json-10k", "db", "articles", "article", "auth-me", "auth-articles",
    ]  # fmt: skip
    assert {method for method, _ in run.SCENARIOS.values()} == {"GET"}
    assert run.SCENARIOS["auth-articles"][1] == "/auth/articles?page=1&page_size=20"
    assert set(run.SCENARIOS) > run.AUTH_SCENARIOS


def test_javascript_mode_runs_django_bolts_three_javascript_servers(
    tmp_path: Path,
) -> None:
    servers = run.SERVER_MODES["javascript"]

    assert list(servers) == ["elysia-bun", "hono-bun", "hono-node"]
    assert not set(servers) & {
        name for mode, table in run.SERVER_MODES.items() if mode != "javascript"
        for name in table
    }  # fmt: skip
    for name, (command, env) in servers.items():
        assert command[0] in run.RUNTIMES
        assert (run.HERE / command[1]).is_file(), name
        assert env["BENCH_DATABASE_URL"] == run.DB
        assert env["BENCH_POOL"] == str(run.POOL)
        assert env["BENCH_WORKERS"] == str(run.WORKERS)
        assert env["BENCH_PORT"] == str(run.PORT)
        payloads_path = Path(run.server_env(name, {}, tmp_path)["BENCH_PAYLOADS"])
        assert json.loads(payloads_path.read_text()) == parity_data.PAYLOADS
    assert {name: command[0] for name, (command, _) in servers.items()} == {
        "elysia-bun": "bun",
        "hono-bun": "bun",
        "hono-node": "node",
    }
    assert "BENCH_PAYLOADS" not in run.server_env("hinoki", {}, tmp_path)


def test_only_the_bench_variables_a_stack_declares_reach_its_server() -> None:
    leaked = {
        "PATH": "/bin",
        "BENCH_DATABASE__URL": "postgres://elsewhere",
        "BENCH_DATABASE_URL": "postgres://elsewhere",
        "BENCH_POOL": "3",
        "BENCH_BUN_WORKER": "1",
        "BENCH_POSTGRES_CONTAINER": "other",
    }

    bun = run.server_env("elysia-bun", leaked)
    hinoki = run.server_env("hinoki", leaked)
    kiso = run.server_env("hinoki-kiso", leaked)
    fastapi = run.server_env("fastapi", leaked)

    assert bun["PATH"] == "/bin"
    assert "BENCH_BUN_WORKER" not in bun  # would turn the launcher into a worker
    assert "BENCH_DATABASE__URL" not in bun
    assert (bun["BENCH_DATABASE_URL"], bun["BENCH_POOL"]) == (run.DB, str(run.POOL))
    assert {k for k in hinoki if k.startswith("BENCH_")} == {
        "BENCH_DATABASE__URL",
        "BENCH_DATABASE__MAX_SIZE",
    }
    assert hinoki["BENCH_DATABASE__URL"] == run.DB
    assert "BENCH_SERVER__RUNTIME_THREADS" not in hinoki
    assert "BENCH_POSTGRES_CONTAINER" not in fastapi
    assert fastapi["BENCH_POOL"] == str(run.POOL)
    assert fastapi["PYTHONPATH"] == str(run.HERE)
    assert kiso["BENCH_SERVER__RUNTIME_THREADS"] == "1"


def test_python_servers_run_in_this_interpreter_and_runtimes_by_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run, "RUNTIMES", {"node": "/opt/node/bin/node", "bun": None})

    assert run.server_command(["granian", "--x"]) == [
        sys.executable,
        "-m",
        "granian",
        "--x",
    ]
    assert run.server_command(["python", "manage.py"]) == [sys.executable, "manage.py"]
    assert run.server_command(["node", "js/a.js"]) == ["/opt/node/bin/node", "js/a.js"]
    with pytest.raises(SystemExit, match="bun is not installed"):
        run.server_command(["bun", "js/b.js"])


def test_javascript_versions_come_from_the_runtimes_and_installed_packages(
    tmp_path: Path,
) -> None:
    for name, version in {"elysia": "1.0.0", "@hono/node-server": "2.0.0"}.items():
        package = tmp_path / "node_modules" / name
        package.mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({"version": version}))

    found = run.javascript_versions(tmp_path, {"node": sys.executable, "bun": None})

    assert (
        found["runtimes"]["node"]
        == f"{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}"
    )
    assert found["runtimes"]["bun"] is None
    assert found["packages"]["elysia"] == "1.0.0"
    assert found["packages"]["@hono/node-server"] == "2.0.0"
    assert found["packages"]["pg"] is None


def run_node(script: str) -> str:
    return subprocess.run(
        [run.RUNTIMES["node"], "--input-type=module", "-e", script],
        cwd=run.HERE,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.mark.skipif(run.RUNTIMES.get("node") is None, reason="needs Node.js")
def test_javascript_serializes_the_payloads_to_the_same_bytes(tmp_path: Path) -> None:
    path = run.write_payloads(tmp_path)
    script = (
        "const m = await import('./js/common.js');"
        f"const p = m.loadPayloads({json.dumps(str(path))});"
        "process.stdout.write(JSON.stringify(Object.fromEntries("
        "Object.entries(p).map(([k, v]) => [k, JSON.stringify(v)]))));"
    )

    bodies = {k: v.encode() for k, v in json.loads(run_node(script)).items()}

    assert bodies == parity_data.BODIES


@pytest.mark.skipif(run.RUNTIMES.get("node") is None, reason="needs Node.js")
def test_javascript_accepts_the_benchmark_jwt_and_rejects_others() -> None:
    token = parity_data.mint_token()
    forged = jwt.encode(
        {"sub": "1", "exp": 4102444800}, parity_data.SECRET[::-1], "HS256"
    )
    expired = jwt.encode(
        {"sub": "1", "exp": 1}, parity_data.SECRET, parity_data.ALGORITHM
    )
    script = (
        "const m = await import('./js/common.js');"
        f"const tokens = {json.dumps([token, forged, expired, 'x.y.z', ''])};"
        "process.stdout.write(JSON.stringify(tokens.map((t) => m.tokenSubject(t))));"
    )

    assert json.loads(run_node(script)) == [
        parity_data.AUTH_USER_ID,
        None,
        None,
        None,
        None,
    ]


def test_payloads_are_deterministic_compact_and_the_advertised_size() -> None:
    assert payloads.make_items(11) == payloads.make_items(11)
    assert 900 <= len(payloads.BODIES["json-1k"]) <= 1150
    assert 9200 <= len(payloads.BODIES["json-10k"]) <= 11300
    assert b": " not in payloads.BODIES["json-1k"]
    assert json.loads(payloads.BODIES["json-1k"]) == payloads.ITEMS["json-1k"]


def test_bolt_settings_disable_access_logging_and_compression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BENCH_DATABASE_URL", run.DB)
    monkeypatch.delitem(sys.modules, "parity_dj_settings", raising=False)
    settings = importlib.import_module("parity_dj_settings")

    assert settings.BOLT_COMPRESSION is False
    levels = logging.getLevelNamesMapping()
    loggers = settings.LOGGING["loggers"]
    for name in ("django", "django.server", "django_bolt"):
        assert levels[loggers[name]["level"]] >= logging.WARNING, name
    assert loggers["django.server"]["propagate"] is False
    assert loggers["django_bolt"]["propagate"] is False
    assert levels[settings.LOGGING["root"]["level"]] >= logging.WARNING
