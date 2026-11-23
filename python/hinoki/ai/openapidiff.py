"""Detect changes between two OpenAPI documents that break existing clients."""

from __future__ import annotations

from typing import Any

from hinoki.openapi import operation_of

METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace", "query")
JSON = "application/json"


def breaking_changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Problems an existing client of ``old`` meets against ``new``.

    Additions are compatible. Removed operations or success responses, new or newly
    required request input, removed response properties, changed types and narrowed
    request or widened response enums are reported."""
    found: list[str] = []
    new_paths = new.get("paths", {})
    for path, item in old.get("paths", {}).items():
        for method in METHODS:
            before = operation_of(item, method)
            if before is None:
                continue
            label = f"{method.upper()} {path}"
            after = operation_of(new_paths.get(path, {}), method)
            if after is None:
                found.append(f"removed operation {label}")
            else:
                Comparison(old, new, found).operation(label, before, after)
    return found


class Comparison:
    def __init__(self, old: dict[str, Any], new: dict[str, Any], out: list[str]):
        self.old = old
        self.new = new
        self.out = out
        self.seen: set[tuple[str, str]] = set()

    def operation(self, label: str, old: dict[str, Any], new: dict[str, Any]) -> None:
        self.request(label, old, new)
        self.responses(label, old.get("responses", {}), new.get("responses", {}))
        self.parameters(label, old.get("parameters", []), new.get("parameters", []))

    def request(self, label: str, old: dict[str, Any], new: dict[str, Any]) -> None:
        before = body_schema(old.get("requestBody"))
        after = body_schema(new.get("requestBody"))
        if before is not None and after is not None:
            self.seen = set()
            self.schema(f"{label} request body", before, after, incoming=True)

    def responses(self, label: str, old: dict[str, Any], new: dict[str, Any]) -> None:
        for status, response in old.items():
            if status not in new:
                if status.startswith("2"):
                    self.out.append(f"{label}: removed response {status}")
                continue
            before = body_schema(response)
            after = body_schema(new[status])
            if before is not None and after is not None:
                self.seen = set()
                self.schema(f"{label} response {status}", before, after, incoming=False)

    def parameters(
        self, label: str, old: list[dict[str, Any]], new: list[dict[str, Any]]
    ) -> None:
        known = {(p["in"], p["name"]) for p in old}
        for parameter in new:
            key = (parameter["in"], parameter["name"])
            if parameter.get("required") and key not in known:
                self.out.append(f"{label}: new required {key[0]} parameter {key[1]!r}")

    def schema(
        self, where: str, old: dict[str, Any], new: dict[str, Any], incoming: bool
    ) -> None:
        before, after = self.resolve(old, self.old), self.resolve(new, self.new)
        pair = (str(old.get("$ref", "")), str(new.get("$ref", "")))
        if pair != ("", ""):
            if pair in self.seen:
                return
            self.seen.add(pair)
        if self.type_changed(where, before, after):
            return
        self.enum(where, before, after, incoming)
        self.properties(where, before, after, incoming)
        if "items" in before and "items" in after:
            self.schema(f"{where}[]", before["items"], after["items"], incoming)

    def type_changed(
        self, where: str, before: dict[str, Any], after: dict[str, Any]
    ) -> bool:
        if "type" in before and "type" in after and before["type"] != after["type"]:
            self.out.append(
                f"{where}: type changed from {kind(before['type'])} "
                f"to {kind(after['type'])}"
            )
            return True
        return False

    def enum(
        self, where: str, before: dict[str, Any], after: dict[str, Any], incoming: bool
    ) -> None:
        if "enum" not in before or "enum" not in after:
            return
        if incoming:
            lost = [v for v in before["enum"] if v not in after["enum"]]
            if lost:
                self.out.append(f"{where}: enum lost values {lost}")
            return
        gained = [v for v in after["enum"] if v not in before["enum"]]
        if gained:
            self.out.append(f"{where}: enum gained values {gained}")

    def properties(
        self, where: str, before: dict[str, Any], after: dict[str, Any], incoming: bool
    ) -> None:
        old_props = before.get("properties", {})
        new_props = after.get("properties", {})
        if incoming:
            self.required(where, before, after)
        else:
            for name in old_props:
                if name not in new_props:
                    self.out.append(f"{where}: removed property {name!r}")
        for name in old_props:
            if name in new_props:
                self.schema(
                    f"{where}.{name}", old_props[name], new_props[name], incoming
                )

    def required(
        self, where: str, before: dict[str, Any], after: dict[str, Any]
    ) -> None:
        known = before.get("properties", {})
        was = set(before.get("required", []))
        for name in after.get("required", []):
            if name in was:
                continue
            if name in known:
                self.out.append(f"{where}: property {name!r} became required")
            else:
                self.out.append(f"{where}: new required property {name!r}")

    def resolve(
        self, schema: dict[str, Any], document: dict[str, Any]
    ) -> dict[str, Any]:
        ref = schema.get("$ref")
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return schema
        target: Any = document
        for part in ref[2:].split("/"):
            target = target.get(part, {}) if isinstance(target, dict) else {}
        return target if isinstance(target, dict) else {}


def body_schema(body: dict[str, Any] | None) -> dict[str, Any] | None:
    if not body:
        return None
    schema = body.get("content", {}).get(JSON, {}).get("schema")
    return schema if isinstance(schema, dict) else None


def kind(value: Any) -> str:
    return "/".join(value) if isinstance(value, list) else str(value)
