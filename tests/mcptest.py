"""The development MCP server: protocol, tools and isolation."""

import contextlib
import datetime
import io
import json
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from hinoki import Hinoki, mcp
from hinoki.mcp import DevProject, Server, serve
from hinoki.modules import Module, ModuleContext
from hinoki.proposals import ProposalError, propose
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
        "explain_error",
        "check_project",
        "propose_model",
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
    assert (
        result["structuredContent"]["error"]
        == "RuntimeError (details on the server's stderr)"
    )
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
        [sys.executable, "-m", "hinoki", "mcp", "tests.projects.nope:x"],
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
        "from hinoki import Hinoki\n"
        "Hinoki().asgi()\n"
        "print('hinoki.mcp' in sys.modules)"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert done.stdout.strip() == "False"


VERBS = {"list", "get", "create", "update", "delete"}


def test_propose_model_generates_source_that_imports_and_registers() -> None:
    result = propose(
        "Article",
        None,
        [
            {"name": "title", "type": "str", "max_length": 80, "unique": True},
            {"name": "views", "type": "int"},
            {"name": "note", "type": "str", "nullable": True},
        ],
    )
    space: dict[str, object] = {}
    exec(compile(result["source"], "<proposal>", "exec"), space)  # noqa: S102
    app = Hinoki()
    app.include(
        *[v for k, v in space.items() if k.endswith("_article") or "articles" in k]
    )
    assert len(app.routes) == 5
    assert "TODO: add access" in result["source"]
    for bad in (
        {"name": "id"},
        {"name": "Title"},
        {"name": "x", "type": "blob"},
        {"name": "class"},
        {"name": "n", "type": "int", "max_length": 5},
    ):
        with pytest.raises(ProposalError):
            propose("Article", None, [bad])
    with pytest.raises(ProposalError):
        propose("article", None, [{"name": "x"}])
    with pytest.raises(ProposalError):
        propose("A", "bad table", [{"name": "x"}])


def scenario_server(tmp_path: Path, mode: str) -> Server:
    project = DevProject(
        create_app(), rules, waivers=tmp_path / "none.toml", today=TODAY, scenarios=mode
    )
    return Server(project)


def test_run_scenario_is_hidden_and_refused_unless_enabled(
    server: Server, tmp_path: Path
) -> None:
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert listed is not None
    assert "run_scenario" not in {t["name"] for t in listed["result"]["tools"]}
    refused = call(server, "run_scenario", path="/health")["result"]
    assert refused["isError"] and "off" in refused["structuredContent"]["error"]
    enabled = scenario_server(tmp_path, "read")
    answer = enabled.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert answer is not None
    tool = next(t for t in answer["result"]["tools"] if t["name"] == "run_scenario")
    assert tool["annotations"]["readOnlyHint"] is False  # it runs real handlers


def test_run_scenario_runs_a_get_and_refuses_unsafe_requests(tmp_path: Path) -> None:
    server = scenario_server(tmp_path, "read")
    ok = call(server, "run_scenario", path="/health")["result"]
    assert not ok["isError"]
    assert ok["structuredContent"]["status"] == 200
    missing = call(server, "run_scenario", path="/nope")["result"]["structuredContent"]
    assert missing["status"] == 404
    for bad in (
        {"method": "POST", "path": "/items"},
        {"path": "health"},
        {"path": "/health?x=1"},
        {"path": "/health", "headers": {"Host": "evil"}},
        {"path": "/health", "headers": {"x": "a\r\nb: c"}},
        {"method": "TRACE", "path": "/health"},
    ):
        result = call(server, "run_scenario", **bad)["result"]
        assert result["isError"], bad


def test_run_scenario_write_mode_allows_other_methods(tmp_path: Path) -> None:
    server = scenario_server(tmp_path, "write")
    result = call(server, "run_scenario", method="POST", path="/items", json={})
    assert result["result"]["structuredContent"]["status"] in {200, 201, 401, 403, 422}


class Failing(Module):
    name = "failing"

    @contextlib.asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        raise RuntimeError("could not connect to postgres://user:s3cret@db/prod")
        yield


def test_a_failing_startup_returns_an_error_without_hanging_or_leaking(
    tmp_path: Path,
) -> None:
    app = create_app()
    app.install(Failing())
    project = DevProject(
        app, rules, waivers=tmp_path / "none.toml", today=TODAY, scenarios="read"
    )
    result = call(Server(project), "run_scenario", path="/health")["result"]
    assert result["isError"]
    assert "s3cret" not in json.dumps(result)


def test_scenario_input_hygiene_and_honest_annotations(tmp_path: Path) -> None:
    server = scenario_server(tmp_path, "read")
    for bad in (
        {"method": ["GET"], "path": "/health"},
        {"method": "get", "path": "/health"},
        {"path": "/health", "query": "a=1\r\nX: y"},
        {"path": "/health", "query": "a#b"},
        {"path": "/health", "headers": {"a b": "x"}},
        {"path": "/health", "headers": {"é": "x"}},
    ):
        assert call(server, "run_scenario", **bad)["result"]["isError"], bad
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert listed is not None
    tool = next(t for t in listed["result"]["tools"] if t["name"] == "run_scenario")
    assert tool["annotations"]["readOnlyHint"] is False


def test_propose_model_rejects_odd_inputs() -> None:
    for bad in (
        {"name": "x", "type": ["str"]},
        {"name": "x", "unique": "false"},
        {"name": "x", "max_length": 10**5000},
        {"name": "x", "max_length": True},
        {"name": "datetime"},
        {"name": "_hidden"},
        {"name": "é"},
        {"name": "a" * 65},
        {"name": "dump"},
    ):
        with pytest.raises(ProposalError):
            propose("Article", None, [bad])
    for model in ("Model", "UUID", "Schema", "\uff21rticle"):
        with pytest.raises(ProposalError):
            propose(model, None, [{"name": "x"}])


def test_explain_error_knows_builtin_rules_and_more_codes(server: Server) -> None:
    for code in ("HNK-API-001", "HNK-SCH-001", "conflict", "ge", "unique_conflict"):
        result = call(server, "explain_error", code=code)["result"]
        assert not result["isError"], code
        assert result["structuredContent"]["meaning"]


def load(source: str) -> list[object]:
    space: dict[str, object] = {}
    exec(compile(source, "<proposal>", "exec"), space)  # noqa: S102
    return [v for k, v in space.items() if k.split("_")[0] in VERBS]


def test_propose_model_with_an_owner_scopes_every_query_and_paginates() -> None:
    result = propose(
        "Bookmark",
        "bookmarks",
        [{"name": "url", "max_length": 500}, {"name": "tags", "nullable": True}],
        owner="Member",
        unique_together=[["owner_id", "url"]],
    )
    source = result["source"]
    assert source.count("owner_id=caller.user.id") >= 5
    assert "unique=[['owner_id', 'url']]" in source
    assert "PageNumberPagination" in source and "access=member_only" in source
    assert "TODO: add access" not in source
    compile(source, "<proposal>", "exec")
    with pytest.raises(ProposalError):
        propose("A", None, [{"name": "x"}], path="/Bad Path")
    with pytest.raises(ProposalError):
        propose("A", None, [{"name": "x"}], unique_together=[["nope"]])
    with pytest.raises(ProposalError):
        propose("A", None, [{"name": "owner_id"}], owner="Member")
