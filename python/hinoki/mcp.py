"""Development MCP server: structured, read-only context about a project.

``python -m hinoki mcp --app module:factory`` speaks JSON-RPC 2.0 over stdio
(one JSON message per line, as in the MCP stdio transport). It is a separate
command and nothing in ``hinoki.runtime`` imports this module, so a production
server cannot publish it by accident. All tools are read-only; there is no tool
to change rules, waivers or code, and checks with side effects are never run.
Every result carries ``tool_version``.

Not verified against a real MCP client: the method names and result shapes
follow the published specification as the author knows it; run a client against
it before relying on compatibility.
"""

from __future__ import annotations

import argparse
import datetime
import inspect
import json
import sys
import typing
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

import hinoki
from hinoki.app import HinokiAPI
from hinoki.checkcli import USAGE_ERRORS, build_app, build_rules
from hinoki.migrations import models_of
from hinoki.orm import Model
from hinoki.routing import Route
from hinoki.rules import Diagnostic, Phase, Project, RuleSet, failing, qualified, where
from hinoki.waivers import NAME, apply_waivers, load_waivers

TOOL_VERSION = 1
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
PARSE_ERROR, INVALID_REQUEST, NO_METHOD, INVALID_PARAMS = -32700, -32600, -32601, -32602
INTERNAL_ERROR = -32603

Handler = Callable[[dict[str, Any]], dict[str, Any]]


class RpcError(Exception):
    """A protocol-level fault, answered with a JSON-RPC error."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class ToolFailure(Exception):
    """A tool could not do what was asked; reported as ``isError``, not a crash."""


def names_of(route: Route) -> set[str]:
    """Names an action answers to: full id, ``Class.method``, and the bare method
    name (which may be ambiguous)."""
    owner = route.owner.__qualname__ if route.owner is not None else ""
    short = f"{owner}.{route.handler.__name__}" if owner else route.handler.__qualname__
    return {qualified(route), short, route.handler.__name__}


def describe_access(route: Route) -> dict[str, Any] | None:
    if route.access is None:
        return None
    return {
        "provider": type(route.access.auth).__name__,
        "scopes": sorted(str(s) for s in route.access.scopes),
    }


def describe_action(route: Route) -> dict[str, Any]:
    return {
        "id": qualified(route),
        "method": route.method,
        "path": route.path,
        "status": route.status,
        "access": describe_access(route),
        "transaction": route.transaction,
        "pagination": route.pagination is not None,
        "parsers": [p.media_type for p in route.parsers],
        "renderers": [r.media_type for r in route.renderers],
        "location": where(route.handler),
    }


def describe_model(model: type[Model]) -> dict[str, Any]:
    hints = typing.get_type_hints(model)
    return {
        "table": model.__table__,
        "name": model.__name__,
        "primary_key": model.__primary_key__,
        "columns": {name: str(hints[name]) for name in model.__columns__},
        "relations": {
            name: {"to": rel.model.__name__, "forward": rel.forward}
            for name, rel in model.__relations__.items()
        },
        "unique": [list(group) for group in model.__unique__],
    }


class DevProject:
    """What the server knows: the app, its rules and optional extras."""

    def __init__(
        self,
        app: HinokiAPI,
        rules: RuleSet,
        packages: tuple[str, ...] = (),
        models: tuple[str, ...] = (),
        waivers: Path | None = None,
        today: datetime.date | None = None,
    ) -> None:
        self.app = app
        self.rules = rules
        self.packages = packages
        self.model_modules = models
        self.waivers = waivers or Path(NAME)
        self.today = today

    def find(self, name: str) -> Route:
        matches = [r for r in self.app.routes if name in names_of(r)]
        if not matches:
            raise ToolFailure(f"no action named {name!r}")
        if len(matches) > 1:
            ids = sorted(qualified(r) for r in matches)
            raise ToolFailure(f"{name!r} is ambiguous: {ids}")
        return matches[0]

    def diagnostics(
        self, phase: Phase | None
    ) -> tuple[list[dict[str, str]], list[Diagnostic]]:
        """All diagnostics as data, and the ones that fail a build."""
        found = self.rules.run(Project(self.app, self.packages), phase)
        today = self.today or datetime.date.today()
        found = apply_waivers(found, load_waivers(self.waivers), today)
        return [d.as_dict() for d in found], failing(found)


def tool_inspect_project(project: DevProject, _args: dict[str, Any]) -> dict[str, Any]:
    models = [
        describe_model(m) for module in project.model_modules for m in models_of(module)
    ]
    return {
        "title": project.app.title,
        "version": project.app.version,
        "actions": [describe_action(r) for r in project.app.routes],
        "models": models,
        "rules": len(project.rules.rules),
    }


def tool_list_rules(project: DevProject, _args: dict[str, Any]) -> dict[str, Any]:
    waivers = load_waivers(project.waivers)
    return {
        "waiver_file": {
            "path": str(project.waivers.resolve()),
            "exists": project.waivers.is_file(),
        },
        "rules": [rule.describe() for rule in project.rules.rules],
        "waivers": [
            {
                "rule": w.rule,
                "target": w.target,
                "reason": w.reason,
                "owner": w.owner,
                "expires": w.expires.isoformat() if w.expires else None,
            }
            for w in waivers
        ],
    }


def tool_explain_action(project: DevProject, args: dict[str, Any]) -> dict[str, Any]:
    name = args.get("action")
    if not isinstance(name, str) or not name:
        raise ToolFailure("'action' must be the name of an action")
    route = project.find(name)
    operation = (
        project.app.openapi()["paths"].get(route.path, {}).get(route.method.lower(), {})
    )
    diagnostics, _ = project.diagnostics(None)
    subject = qualified(route)
    return {
        **describe_action(route),
        "signature": str(inspect.signature(route.handler)),
        "documentation": inspect.getdoc(route.handler) or "",
        "operation": operation,
        "diagnostics": [
            d
            for d in diagnostics
            if d["subject"] == subject or d["subject"].startswith(subject + ".")
        ],
    }


def tool_check_project(project: DevProject, args: dict[str, Any]) -> dict[str, Any]:
    phase = args.get("phase")
    if phase is not None and phase not in {p.value for p in Phase}:
        raise ToolFailure("'phase' must be 'static' or 'startup'")
    diagnostics, broken = project.diagnostics(Phase(phase) if phase else None)
    return {
        "ok": not broken,
        "failing": len(broken),
        "diagnostics": diagnostics,
        "note": "checks with side effects are never run through this server",
    }


TOOLS: dict[str, tuple[str, dict[str, Any], Callable[..., dict[str, Any]]]] = {
    "inspect_project": (
        "Actions (method, path, access, transaction, parsers, renderers, location), "
        "models if configured, and the number of enabled rules.",
        {"type": "object", "properties": {}},
        tool_inspect_project,
    ),
    "list_rules": (
        "Enabled rules with stable IDs, phases, severities and explanations, plus "
        "the owner-declared waivers (read only).",
        {"type": "object", "properties": {}},
        tool_list_rules,
    ),
    "explain_action": (
        "Everything known about one action: signature, documentation, access, the "
        "OpenAPI operation and the rule diagnostics that concern it.",
        {
            "type": "object",
            "properties": {"action": {"type": "string"}},
            "required": ["action"],
        },
        tool_explain_action,
    ),
    "check_project": (
        "Run the enabled rules (startup and static phases) and return the same "
        "diagnostics as `python -m hinoki check`, with waivers applied.",
        {
            "type": "object",
            "properties": {"phase": {"type": "string", "enum": ["static", "startup"]}},
        },
        tool_check_project,
    ),
}


def problem_of(message: dict[str, Any]) -> tuple[Any, int, str] | None:
    """``(id, code, text)`` for the first thing wrong with a request."""
    identifier = message.get("id")
    if "id" in message and (
        isinstance(identifier, bool) or not isinstance(identifier, str | int)
    ):
        return None, INVALID_REQUEST, "id must be a string or an integer"
    if not isinstance(message.get("method"), str):
        return identifier, INVALID_REQUEST, "missing method"
    if "params" in message and not isinstance(message["params"], dict):
        return identifier, INVALID_PARAMS, "params must be an object"
    return None


def check_message(message: Any) -> dict[str, Any] | None:
    """An error response when ``message`` is not a valid request, else ``None``.
    An invalid notification gets no answer at all."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return failure(None, INVALID_REQUEST, "expected a JSON-RPC 2.0 object")
    found = problem_of(message)
    if found is None or ("id" not in message and found[1] != INVALID_REQUEST):
        return None
    return failure(*found)


def reply(identifier: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


def failure(identifier: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": identifier,
        "error": {"code": code, "message": message},
    }


class Server:
    """Message handler, independent of the transport (testable without pipes)."""

    def __init__(self, project: DevProject) -> None:
        self.project = project

    def handle(self, message: Any) -> dict[str, Any] | None:
        """The response for one decoded message, or ``None`` for a notification."""
        problem = check_message(message)
        if problem is not None:
            return problem
        if "id" not in message:
            return None  # notifications get no answer
        identifier, params = message["id"], message.get("params") or {}
        try:
            action = self.methods().get(message["method"])
            if action is None:
                raise RpcError(NO_METHOD, f"unknown method {message['method']!r}")
            return reply(identifier, action(params))
        except RpcError as exc:
            return failure(identifier, exc.code, str(exc))

    def methods(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return {
            "initialize": self.initialize,
            "ping": lambda _params: {},
            "tools/list": lambda _params: {"tools": self.tool_list()},
            "tools/call": self.call,
        }

    @staticmethod
    def initialize(params: dict[str, Any]) -> dict[str, Any]:
        wanted = params.get("protocolVersion")
        return {
            "protocolVersion": wanted if wanted in PROTOCOLS else PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "hinoki-dev", "version": hinoki.__version__},
            "instructions": "Read-only context about a Hinoki project. Rule "
            "waivers and code are changed by the project owner, not by tools.",
        }

    @staticmethod
    def tool_list() -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "description": description,
                "inputSchema": schema,
                "annotations": {"readOnlyHint": True, "openWorldHint": False},
            }
            for name, (description, schema, _) in TOOLS.items()
        ]

    def call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or name not in TOOLS:
            raise RpcError(INVALID_PARAMS, f"unknown tool {name!r}")
        if not isinstance(arguments, dict):
            raise RpcError(INVALID_PARAMS, "arguments must be an object")
        try:
            data = {
                "tool_version": TOOL_VERSION,
                **TOOLS[name][2](self.project, arguments),
            }
            error = False
        except ToolFailure as exc:
            data, error = {"tool_version": TOOL_VERSION, "error": str(exc)}, True
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            data, error = {"tool_version": TOOL_VERSION, "error": message}, True
        data = json.loads(json.dumps(data, default=str))  # always plain JSON
        return {
            "content": [{"type": "text", "text": json.dumps(data, indent=2)}],
            "structuredContent": data,
            "isError": error,
        }


def answer_line(server: Server, line: str) -> dict[str, Any] | None:
    """The response for one input line; no input can make this raise."""
    try:
        message = json.loads(line)
    except (ValueError, RecursionError):
        return failure(None, PARSE_ERROR, "invalid JSON")
    try:
        return server.handle(message)
    except Exception:  # the loop must survive any one message
        identifier = message.get("id") if isinstance(message, dict) else None
        return failure(identifier, INTERNAL_ERROR, "internal error")


def serve(server: Server, source: TextIO, sink: TextIO) -> None:
    """Newline-delimited JSON-RPC until ``source`` ends. Only protocol messages
    are written to ``sink``."""
    for line in source:
        if not line.strip():
            continue
        answer = answer_line(server, line)
        if answer is not None:
            sink.write(json.dumps(answer) + "\n")
            sink.flush()


def run_mcp(args: argparse.Namespace) -> int:
    """``python -m hinoki mcp``: serve on stdin/stdout until stdin closes."""
    try:
        app = build_app(args.app)
        project = DevProject(
            app,
            build_rules(args.rules, app),
            tuple(args.package or ()),
            tuple(args.models or ()),
            Path(args.waivers) if args.waivers else None,
        )
    except (*USAGE_ERRORS, SystemExit) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # Application code run by rules must not write into the protocol stream.
    protocol, sys.stdout = sys.stdout, sys.stderr
    try:
        serve(Server(project), sys.stdin, protocol)
    finally:
        sys.stdout = protocol
    return 0
