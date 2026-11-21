import json
import signal
import socket
import subprocess
import sys
from pathlib import Path

import loadtool  # type: ignore[import-not-found]
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


def test_scenarios_include_the_payload_size_endpoints_and_keep_the_old_ones() -> None:
    assert {"health", "list", "detail", "create"} <= set(run.SCENARIOS)
    assert run.SCENARIOS["json-1k"] == ("GET", "/json-1k", None)
    assert run.SCENARIOS["json-10k"] == ("GET", "/json-10k", None)


def test_payloads_are_deterministic_compact_and_the_advertised_size() -> None:
    assert payloads.make_items(11) == payloads.make_items(11)
    assert 900 <= len(payloads.BODIES["json-1k"]) <= 1150
    assert 9200 <= len(payloads.BODIES["json-10k"]) <= 11300
    assert b": " not in payloads.BODIES["json-1k"]
    assert json.loads(payloads.BODIES["json-1k"]) == payloads.ITEMS["json-1k"]
