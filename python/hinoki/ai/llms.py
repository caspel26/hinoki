"""Render an application's OpenAPI document as an llms.txt Markdown text.

The text is derived only from the document that ``Hinoki.openapi()`` builds, so it
leaves out whatever the document leaves out (``include_in_schema=False``, the
admin's pages) and never reads a route directly. The format is the one described
at https://llmstxt.org: a title, a blockquote, then sections of links and lists.
"""

from __future__ import annotations

from collections.abc import Mapping

FRAMEWORK_DOCS = "https://hinoki.caspel.dev/llms.txt"
MEDIA_TYPE = "text/markdown; charset=utf-8"
# The path item fields that hold an operation, in the order they are listed.
# ``x-query`` is where a document written as OpenAPI 3.1 puts a QUERY operation.
METHOD_FIELDS = (
    ("get", "GET"),
    ("post", "POST"),
    ("put", "PUT"),
    ("patch", "PATCH"),
    ("delete", "DELETE"),
    ("query", "QUERY"),
    ("x-query", "QUERY"),
    ("head", "HEAD"),
    ("options", "OPTIONS"),
    ("trace", "TRACE"),
)
UNTAGGED = "Operations"
SCHEMA_PREFIX = "#/components/schemas/"


def render_llms(
    document: Mapping[str, object], *, openapi_url: str | None = None
) -> str:
    """The llms.txt Markdown for an OpenAPI ``document``, with a trailing newline.

    ``openapi_url`` is linked as the machine-readable document when the
    application serves it. The output is deterministic: sections are sorted by
    tag, operations by path and then by method."""
    info = mapping(document.get("info"))
    lines = [f"# {line(info.get('title')) or 'API'}", ""]
    description = line(info.get("description"))
    if description:
        lines.append(f"> {description}")
    version = line(info.get("version"))
    if version:
        lines.append(f"> Version {version}")
    lines += [
        "",
        f"Built with Hinoki; framework docs for agents: {FRAMEWORK_DOCS}",
    ]
    if openapi_url:
        lines.append(f"OpenAPI document: [{openapi_url}]({openapi_url})")
    for title, entries in sections(document).items():
        lines += ["", f"## {title}", "", *entries]
    sockets = websockets(document)
    if sockets:
        lines += ["", "## WebSockets", "", *sockets]
    return "\n".join(lines) + "\n"


def mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def line(value: object) -> str:
    """``value`` as one line of text: no newline can start a Markdown block."""
    return " ".join(value.split()) if isinstance(value, str) else ""


def sections(document: Mapping[str, object]) -> dict[str, list[str]]:
    """The operation lines under the operation's first tag, tags sorted and the
    untagged operations last."""
    found: dict[str, list[str]] = {}
    inherited = document.get("security")
    paths = mapping(document.get("paths"))
    for path in sorted(paths):
        item = mapping(paths[path])
        for field, method in METHOD_FIELDS:
            operation = item.get(field)
            if isinstance(operation, Mapping):
                title = first_tag(operation)
                row = entry(method, path, operation, inherited)
                found.setdefault(title, []).append(row)
    return dict(sorted(found.items(), key=lambda pair: (pair[0] == UNTAGGED, pair[0])))


def first_tag(operation: Mapping[str, object]) -> str:
    tags = operation.get("tags")
    if isinstance(tags, list) and tags:
        return line(tags[0]) or UNTAGGED
    return UNTAGGED


def entry(
    method: str, path: str, operation: Mapping[str, object], inherited: object
) -> str:
    text = f"- `{method} {line(path)}`"
    identifier = line(operation.get("operationId"))
    if identifier:
        text += f" ({identifier})"
    summary = line(operation.get("summary"))
    if summary:
        text += f": {summary}"
    facts = operation_facts(operation, inherited)
    if facts:
        text += f" — {'; '.join(facts)}"
    return text


def operation_facts(operation: Mapping[str, object], inherited: object) -> list[str]:
    """The facts of an operation. Without its own ``security`` it inherits the
    document's; an empty list there makes it public."""
    facts = ["deprecated"] if operation.get("deprecated") is True else []
    schemes = security_names(operation.get("security", inherited))
    if schemes:
        facts.append(f"security: {', '.join(schemes)}")
    request = request_schemas(operation.get("requestBody"))
    if request:
        facts.append(f"request: {', '.join(request)}")
    response = success_response(operation.get("responses"))
    if response:
        facts.append(f"response: {response}")
    return facts


def security_names(requirements: object) -> list[str]:
    """The names of the security schemes an operation requires; never a credential."""
    if not isinstance(requirements, list):
        return []
    names = {
        line(name)
        for requirement in requirements
        if isinstance(requirement, Mapping)
        for name in requirement
    }
    return sorted(names - {""})


def request_schemas(body: object) -> list[str]:
    """The component names of a request body, one per media type that has one."""
    content = mapping(mapping(body).get("content"))
    names = {schema_name(mapping(media).get("schema")) for media in content.values()}
    return sorted(names - {""})


def success_response(responses: object) -> str:
    """The lowest 2xx status with the component its body uses, if any."""
    found = mapping(responses)
    statuses = sorted(
        status for status in found if isinstance(status, str) and status[:1] == "2"
    )
    if not statuses:
        return ""
    content = mapping(mapping(found[statuses[0]]).get("content"))
    names = sorted(
        {schema_name(mapping(media).get("schema")) for media in content.values()} - {""}
    )
    return " ".join([statuses[0], *names[:1]]) if names else statuses[0]


def schema_name(schema: object) -> str:
    """The component a schema names, written ``list of X`` for an array of it."""
    found = mapping(schema)
    reference = found.get("$ref")
    if isinstance(reference, str) and reference.startswith(SCHEMA_PREFIX):
        return line(reference[len(SCHEMA_PREFIX) :])
    if found.get("type") == "array":
        inner = schema_name(found.get("items"))
        return f"list of {inner}" if inner else ""
    return ""


def websockets(document: Mapping[str, object]) -> list[str]:
    """The WebSocket actions the document lists under ``x-hinoki-websockets``."""
    described = mapping(document.get("x-hinoki-websockets"))
    lines = []
    for path in sorted(described):
        action = mapping(described[path])
        text = f"- `WEBSOCKET {line(path)}`"
        identifier = line(action.get("operationId"))
        if identifier:
            text += f" ({identifier})"
        summary = line(action.get("summary"))
        if summary:
            text += f": {summary}"
        lines.append(text)
    return lines
