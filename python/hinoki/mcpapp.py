"""Publish selected actions of a running application as MCP tools.

    mcp = expose_as_mcp(app, tools=["shop.api.list_orders"], access=agent_access)
    app.include(*mcp)

See ``docs/APP-MCP-DESIGN.md``. A tool call is an in-process request through
the application's own ASGI adapter with the caller's credentials, so access
policies, validation, limits and transactions apply exactly as for any client.
"""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import quote, unquote, urlencode

import hinoki
from hinoki import context
from hinoki.access import Access
from hinoki.app import Hinoki
from hinoki.core import decode_json
from hinoki.mcp import (
    INVALID_PARAMS,
    NO_METHOD,
    PARSE_ERROR,
    PROTOCOLS,
    RpcError,
    check_message,
    failure,
    reply,
)
from hinoki.reply import Reply
from hinoki.request import Request
from hinoki.routing import Route, post
from hinoki.rules import qualified

SAFE = frozenset({"GET", "HEAD"})
FORWARDED = ("authorization", "cookie", "x-api-key", "host", "x-forwarded-for")
MAX_OUTPUT = 100_000
MAX_MESSAGE = 1_000_000
HTTP_ERROR = 400
DEFS = "$defs"
MARKER = "__hinoki_mcp_endpoint__"


class ExposureError(ValueError):
    """The MCP exposure is configured wrongly (raised when it is built)."""


class FeedBody:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __call__(self) -> dict[str, Any]:
        return {"type": "http.request", "body": self.body, "more_body": False}


def rewrite_refs(value: Any) -> Any:
    """OpenAPI ``#/components/schemas/X`` references as ``#/$defs/X``."""
    if isinstance(value, dict):
        return {
            key: (
                item.replace("#/components/schemas/", f"#/{DEFS}/")
                if key == "$ref" and isinstance(item, str)
                else rewrite_refs(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [rewrite_refs(item) for item in value]
    return value


def declared_errors(route: Route) -> str:
    """A sentence listing the errors the action declares, for the tool description."""
    if not route.errors:
        return ""
    shown = ", ".join(f"{int(e.status)} {e.code}" for e in route.errors)
    return f"\n\nMay fail with: {shown}."


def text_of(value: Any, name: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float | str):
        return str(value)
    raise ValueError(f"argument {name!r} must be text, number or boolean")


def place(
    path: str, name: str, where: str, value: Any
) -> tuple[str, list[tuple[str, str]]]:
    """The path with a path argument substituted, or query pairs for a query one."""
    items = value if isinstance(value, list) else [value]
    texts = [text_of(item, name) for item in items]
    if where != "path":
        return path, [(name, text) for text in texts]
    if len(texts) != 1:
        raise ValueError(f"{name!r} takes one value")
    return path.replace("{" + name + "}", quote(texts[0], safe="")), []


class Tool:
    """One exposed action: its public description and how to call it."""

    def __init__(
        self, route: Route, operation: dict[str, Any], defs: dict[str, Any]
    ) -> None:
        self.route = route
        self.name = str(operation["operationId"])
        text = operation.get("description") or operation.get("summary") or ""
        self.description = (str(text) + declared_errors(route))[:2000]
        self.parameters = {
            p["name"]: p
            for p in operation.get("parameters", [])
            if p.get("in") in {"path", "query"}
        }
        request_body = operation.get("requestBody", {})
        body = request_body.get("content", {}).get("application/json", {})
        self.body_schema = rewrite_refs(body.get("schema")) if body else None
        self.body_required = bool(request_body.get("required"))
        properties = {
            name: rewrite_refs(p.get("schema", {}))
            for name, p in self.parameters.items()
        }
        required = [n for n, p in self.parameters.items() if p.get("required")]
        if self.body_schema is not None:
            properties["body"] = self.body_schema
            if self.body_required:
                required.append("body")
        self.input_schema = {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
            DEFS: rewrite_refs(defs),
        }

    def listing(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {
                "readOnlyHint": self.route.method in SAFE,
                "destructiveHint": self.route.method == "DELETE",
                "openWorldHint": False,
            },
        }

    def request_parts(self, arguments: dict[str, Any]) -> tuple[str, str, bytes]:
        """``(path, query, body)`` for the arguments; ``ValueError`` if invalid."""
        allowed = set(self.parameters)
        if self.body_schema is not None:
            allowed.add("body")
        unknown = sorted(set(arguments) - allowed)
        if unknown:
            raise ValueError(f"unknown arguments {unknown}")
        path = self.route.path
        query: list[tuple[str, str]] = []
        for name, parameter in self.parameters.items():
            if name in arguments:
                path, found = place(path, name, parameter["in"], arguments[name])
                query += found
            elif parameter.get("required"):
                raise ValueError(f"missing argument {name!r}")
        if "body" in arguments:
            return path, urlencode(query), json.dumps(arguments["body"]).encode()
        if self.body_required:
            raise ValueError("missing argument 'body'")
        return path, urlencode(query), b""


def select(
    app: Hinoki, patterns: list[str], allow_writes: bool, mount: str
) -> list[Tool]:
    document = app.openapi()
    defs = document.get("components", {}).get("schemas", {})
    chosen: list[Tool] = []
    for route in app.routes:
        if (
            getattr(route.handler, MARKER, False)
            or route.path == mount
            or not any(
                fnmatch.fnmatchcase(qualified(route), pattern) for pattern in patterns
            )
        ):
            continue
        if route.method not in SAFE and not allow_writes:
            continue
        operation = document["paths"].get(route.path, {}).get(route.method.lower())
        if operation is None:
            raise ExposureError(f"{qualified(route)} is not in the OpenAPI document")
        chosen.append(Tool(route, operation, defs))
    return chosen


async def dispatch(
    app: Hinoki, tool: Tool, parts: tuple[str, str, bytes], request: Request
) -> dict[str, Any]:
    """Run the action in-process with the caller's credentials."""
    path, query, body = parts
    headers = [
        (name.encode(), request.headers[name].encode("latin-1"))
        for name in FORWARDED
        if name in request.headers
    ]
    headers.append((b"accept", b"application/json"))
    if body:
        headers.append((b"content-type", b"application/json"))
    scope = {
        "type": "http",
        "method": tool.route.method,
        "path": unquote(path),  # as a server sends it: decoded, plus the raw form
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": headers,
        "scheme": request.scheme,
        "client": request.client,  # per-client limits and audit see the real peer
    }
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    outer_headers = context.route_headers.get()
    try:
        await app.asgi()(scope, FeedBody(body), send)
    finally:
        context.route_headers.set(outer_headers)  # the inner route's must not leak out
    start = next(m for m in sent if m["type"] == "http.response.start")
    chunks = [m.get("body", b"") for m in sent if m["type"] == "http.response.body"]
    raw = b"".join(chunks)
    result: dict[str, Any] = {
        "content": [
            {"type": "text", "text": raw[:MAX_OUTPUT].decode("utf-8", "replace")}
        ],
        "isError": start["status"] >= HTTP_ERROR,
        "_meta": {"status": start["status"], "truncated": len(raw) > MAX_OUTPUT},
    }
    if raw and len(raw) <= MAX_OUTPUT:
        try:
            parsed = decode_json(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            result["structuredContent"] = parsed
    return result


def refuse(request: Request, origins: frozenset[str]) -> Reply | None:
    """A refusal for requests a browser page could send, or an oversized body."""
    if len(request.body) > MAX_MESSAGE:
        return Reply(413)
    if request.content_type != "application/json":
        return Reply(415)
    origin = request.headers.get("origin")
    cross_site = request.headers.get("sec-fetch-site") == "cross-site"
    if cross_site or (origin is not None and origin not in origins):
        return Reply(403)
    return None


def json_reply(payload: dict[str, Any]) -> Reply:
    return Reply(200, json.dumps(payload), content_type="application/json")


def initialize(params: dict[str, Any], app: Hinoki) -> dict[str, Any]:
    wanted = params.get("protocolVersion")
    return {
        "protocolVersion": wanted if wanted in PROTOCOLS else PROTOCOLS[0],
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": app.title, "version": hinoki.__version__},
    }


async def call_tool(
    params: dict[str, Any], table: dict[str, Tool], app: Hinoki, request: Request
) -> dict[str, Any]:
    name, arguments = params.get("name"), params.get("arguments") or {}
    tool = table.get(name) if isinstance(name, str) else None
    if tool is None or not isinstance(arguments, dict):
        raise RpcError(INVALID_PARAMS, "unknown tool or arguments are not an object")
    try:
        parts = tool.request_parts(arguments)
    except ValueError as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    return await dispatch(app, tool, parts, request)


async def handle(
    message: dict[str, Any], table: dict[str, Tool], app: Hinoki, request: Request
) -> dict[str, Any]:
    method, params = message["method"], message.get("params") or {}
    if method == "tools/call":
        return await call_tool(params, table, app, request)
    simple: dict[str, Callable[[], dict[str, Any]]] = {
        "initialize": lambda: initialize(params, app),
        "ping": dict,
        "tools/list": lambda: {"tools": [t.listing() for t in table.values()]},
    }
    if method not in simple:
        raise RpcError(NO_METHOD, f"unknown method {method!r}")
    return simple[method]()


async def respond(
    request: Request, table: Callable[[], dict[str, Tool]], app: Hinoki
) -> dict[str, Any] | None:
    """The JSON-RPC answer for one POSTed message (``None`` for a notification)."""
    try:
        message = decode_json(request.body)
    except ValueError:
        return failure(None, PARSE_ERROR, "invalid JSON")
    problem = check_message(message)
    if problem is not None or "id" not in message:
        return problem
    try:
        return reply(message["id"], await handle(message, table(), app, request))
    except RpcError as exc:
        return failure(message["id"], exc.code, str(exc))


def expose_as_mcp(
    app: Hinoki,
    *,
    tools: Iterable[str],
    access: Access | None,
    path: str = "/mcp",
    allow_writes: bool = False,
    public: bool = False,
    allowed_origins: Iterable[str] = (),
) -> list[Callable[..., Any]]:
    """The MCP action to ``app.include(...)``, publishing the matching actions.

    ``tools`` are ``fnmatch`` patterns over qualified action names; call this
    after the exposed actions are included. ``access`` protects the endpoint
    (``None`` only with ``public=True``).

    The endpoint answers only ``application/json`` POSTs and refuses a request
    that carries an ``Origin`` outside ``allowed_origins`` (browsers send one;
    agents and CLI clients do not) or ``Sec-Fetch-Site: cross-site``: this keeps
    a web page from driving the tools with the user's cookie and covers the MCP
    DNS-rebinding rule. Cookie authentication together with ``allow_writes`` is
    still a risky combination; prefer bearer credentials."""
    patterns = list(tools)
    if not patterns:
        raise ExposureError("name the actions to expose (tools=[...])")
    if access is None and not public:
        raise ExposureError("protect the endpoint with access=..., or pass public=True")
    if not any(
        fnmatch.fnmatchcase(qualified(route), pattern)
        for route in app.routes
        for pattern in patterns
    ):
        raise ExposureError(f"no included action matches tools={patterns}")
    origins = frozenset(allowed_origins)
    cache: list[Tool] = []

    def tool_table() -> dict[str, Tool]:
        if not cache:  # built once, after every action is registered
            found = select(app, patterns, allow_writes, path)
            if not found:
                raise ExposureError("no action matches the tools patterns")
            cache.extend(found)
        return {t.name: t for t in cache}

    @post(path, access=access)
    async def mcp_endpoint(request: Request) -> Reply:
        """Model Context Protocol endpoint (JSON-RPC over POST)."""
        refused = refuse(request, origins)
        if refused is not None:
            return refused
        answer = await respond(request, tool_table, app)
        return Reply(202) if answer is None else json_reply(answer)

    setattr(mcp_endpoint, MARKER, True)
    return [mcp_endpoint]
