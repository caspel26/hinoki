"""The development MCP server: protocol, tools and isolation."""

import datetime
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from hinoki import mcp
from hinoki.mcp import DevProject, Server, serve
from tests.projects.rulesapp import create_app, rules

TODAY = datetime.date(2026, 11, 13)


@pytest.fixture
def server(tmp_path: Path) -> Server:
    project = DevProject(
        create_app(), rules, waivers=tmp_path / "none.toml", today=TODAY
    )
    return Server(project)


def call(server: Server, name: str, **arguments: Any) -> dict[str, Any]:
    answer = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert answer is not None
    return answer


def test_initialize_negotiates_a_known_protocol_version(server: Server) -> None:
    for asked, given in (("2024-11-05", "2024-11-05"), ("1999-01-01", "2025-06-18")):
        answer = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": asked},
            }
        )
        assert answer is not None
        result = answer["result"]
        assert result["protocolVersion"] == given
        assert result["capabilities"] == {"tools": {"listChanged": False}}
        assert result["serverInfo"]["name"] == "hinoki-dev"


def test_notifications_get_no_answer_and_ping_works(server: Server) -> None:
    assert (
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    )
    assert server.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"}) == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {},
    }


def test_tools_are_listed_as_read_only(server: Server) -> None:
    answer = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert answer is not None
    tools = {t["name"]: t for t in answer["result"]["tools"]}
    assert set(tools) == {
        "inspect_project",
        "list_rules",
        "explain_action",
        "check_project",
    }
    assert all(t["annotations"]["readOnlyHint"] for t in tools.values())
    assert tools["explain_action"]["inputSchema"]["required"] == ["action"]


def test_inspect_project_describes_actions(server: Server) -> None:
    result = call(server, "inspect_project")["result"]
    data = result["structuredContent"]
    assert (result["isError"], data["tool_version"], data["rules"]) == (False, 1, 2)
    ids = {a["id"]: a for a in data["actions"]}
    create = ids["tests.projects.rulesapp.create"]
    assert (create["method"], create["path"], create["access"]) == (
        "POST",
        "/items",
        None,
    )
    assert create["location"].startswith("tests/projects/rulesapp.py:")
    assert json.loads(result["content"][0]["text"]) == data


def test_list_rules_reports_waivers_read_only(tmp_path: Path) -> None:
    waivers = tmp_path / "w.toml"
    waivers.write_text(
        '[[waiver]]\nrule="HNK-ACC-001"\ntarget="tests.projects.*"\nreason="r"\nowner="o"\n'
    )
    server = Server(DevProject(create_app(), rules, waivers=waivers, today=TODAY))
    data = call(server, "list_rules")["result"]["structuredContent"]
    assert [r["id"] for r in data["rules"]] == ["HNK-ACC-001", "HNK-TXN-001"]
    assert data["waivers"][0]["owner"] == "o"
    assert waivers.read_text().count("waiver") == 1  # nothing was written


def test_explain_action_includes_the_rules_that_concern_it(server: Server) -> None:
    data = call(server, "explain_action", action="create")["result"][
        "structuredContent"
    ]
    assert data["id"] == "tests.projects.rulesapp.create"
    assert data["signature"].startswith("(request:")
    assert data["operation"]["responses"]
    assert sorted(d["rule"] for d in data["diagnostics"]) == [
        "HNK-ACC-001",
        "HNK-TXN-001",
    ]
    unknown = call(server, "explain_action", action="nope")["result"]
    assert (
        unknown["isError"]
        and "no action named" in unknown["structuredContent"]["error"]
    )
    missing = call(server, "explain_action")["result"]
    assert missing["isError"]


def test_check_project_matches_the_cli_and_validates_input(server: Server) -> None:
    data = call(server, "check_project")["result"]["structuredContent"]
    assert (data["ok"], data["failing"]) == (False, 3)
    assert {d["status"] for d in data["diagnostics"]} == {"violated"}
    startup = call(server, "check_project", phase="startup")["result"][
        "structuredContent"
    ]
    assert startup["failing"] == 3
    static = call(server, "check_project", phase="static")["result"][
        "structuredContent"
    ]
    assert (static["ok"], static["diagnostics"]) == (True, [])
    bad = call(server, "check_project", phase="runtime")["result"]
    assert bad["isError"]


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ([], -32600),
        ({"jsonrpc": "1.0", "id": 1, "method": "ping"}, -32600),
        ({"jsonrpc": "2.0", "id": 1}, -32600),
        ({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": 3}, -32602),
        ({"jsonrpc": "2.0", "id": 1, "method": "nope"}, -32601),
        (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "x"},
            },
            -32602,
        ),
        (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_rules", "arguments": 3},
            },
            -32602,
        ),
    ],
)
def test_protocol_faults_are_json_rpc_errors(
    server: Server, message: Any, code: int
) -> None:
    answer = server.handle(message)
    assert answer is not None
    assert answer["error"]["code"] == code


def test_a_crashing_tool_is_an_error_result_without_a_traceback(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    description, schema, _ = mcp.TOOLS["list_rules"]
    monkeypatch.setitem(mcp.TOOLS, "list_rules", (description, schema, explode))
    result = call(server, "list_rules")["result"]
    assert result["isError"]
    assert result["structuredContent"]["error"] == "RuntimeError: boom"
    assert "Traceback" not in json.dumps(result)


def test_the_stdio_loop_answers_each_line_and_survives_bad_input(
    server: Server,
) -> None:
    lines = [
        '{"jsonrpc": "2.0", "id": 1, "method": "ping"}',
        "",
        "not json",
        '{"jsonrpc": "2.0", "method": "notifications/initialized"}',
        '{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}',
    ]
    out = io.StringIO()
    serve(server, io.StringIO("\n".join(lines) + "\n"), out)
    answers = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [a["id"] for a in answers] == [1, None, 2]
    assert answers[1]["error"]["code"] == -32700


def test_the_command_serves_over_real_pipes(tmp_path: Path) -> None:
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "hinoki",
            "mcp",
            "--app",
            "tests.projects.rulesapp:create_app",
        ],
        input=request + "\n",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0
    assert json.loads(done.stdout)["result"]["tools"]
    failed = subprocess.run(
        [sys.executable, "-m", "hinoki", "mcp", "--app", "tests.projects.nope:x"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert failed.returncode == 2
    assert failed.stdout == ""


def test_the_production_runtime_never_imports_the_development_server() -> None:
    code = (
        "import sys, hinoki, hinoki.runtime.asgi\n"
        "from hinoki import HinokiAPI\n"
        "HinokiAPI().asgi()\n"
        "print('hinoki.mcp' in sys.modules)"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert done.stdout.strip() == "False"
