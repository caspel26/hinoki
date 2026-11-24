"""Breaking-change detection between two OpenAPI documents."""

import json
from pathlib import Path
from typing import Any

import pytest

from hinoki.__main__ import main
from hinoki.ai.openapidiff import breaking_changes

TARGET = "tests.routeinclusiontest:make_app"


def doc(
    request: dict[str, Any] | None = None,
    response: dict[str, Any] | None = None,
    path: str = "/items",
    method: str = "post",
    parameters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "responses": {
            "200": {
                "description": "ok",
                "content": {
                    "application/json": {"schema": response or {"type": "object"}}
                },
            }
        }
    }
    if request is not None:
        operation["requestBody"] = {
            "content": {"application/json": {"schema": request}}
        }
    if parameters is not None:
        operation["parameters"] = parameters
    return {"openapi": "3.1.0", "paths": {path: {method: operation}}}


def obj(required: list[str] | None = None, **properties: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


def test_query_moving_between_x_query_and_query_is_not_a_removal() -> None:
    old = doc(request=obj(name={"type": "string"}), path="/p", method="x-query")
    new = doc(request=obj(name={"type": "string"}), path="/p", method="query")
    assert breaking_changes(old, new) == []
    assert breaking_changes(new, old) == []
    gone = {"openapi": "3.2.0", "paths": {"/p": {}}}
    assert breaking_changes(old, gone) == ["removed operation QUERY /p"]
    assert breaking_changes(new, gone) == ["removed operation QUERY /p"]


def test_identical_documents_and_additions_are_compatible() -> None:
    old = doc(
        request=obj(name={"type": "string"}), response=obj(id={"type": "integer"})
    )
    new = doc(
        request=obj(name={"type": "string"}, tag={"type": "string"}),
        response=obj(id={"type": "integer"}, extra={"type": "string"}),
    )
    new["paths"]["/other"] = {"get": {"responses": {"200": {"description": "ok"}}}}
    assert breaking_changes(old, old) == []
    assert breaking_changes(old, new) == []


def test_a_removed_operation_or_path_breaks_clients() -> None:
    old = doc()
    assert breaking_changes(old, doc(method="get")) == ["removed operation POST /items"]
    assert breaking_changes(old, doc(path="/moved")) == [
        "removed operation POST /items"
    ]


def test_a_new_required_request_field_breaks_clients() -> None:
    old = doc(request=obj(["name"], name={"type": "string"}))
    new = doc(
        request=obj(["name", "age"], name={"type": "string"}, age={"type": "integer"})
    )
    assert breaking_changes(old, new) == [
        "POST /items request body: new required property 'age'"
    ]


def test_making_a_request_field_required_breaks_clients() -> None:
    old = doc(request=obj(name={"type": "string"}))
    new = doc(request=obj(["name"], name={"type": "string"}))
    assert breaking_changes(old, new) == [
        "POST /items request body: property 'name' became required"
    ]


def test_a_removed_response_property_breaks_clients() -> None:
    old = doc(response=obj(id={"type": "integer"}, name={"type": "string"}))
    new = doc(response=obj(id={"type": "integer"}))
    assert breaking_changes(old, new) == [
        "POST /items response 200: removed property 'name'"
    ]


def test_a_type_change_breaks_in_both_directions() -> None:
    old = doc(request=obj(n={"type": "integer"}), response=obj(n={"type": "integer"}))
    new = doc(request=obj(n={"type": "string"}), response=obj(n={"type": "string"}))
    assert breaking_changes(old, new) == [
        "POST /items request body.n: type changed from integer to string",
        "POST /items response 200.n: type changed from integer to string",
    ]


def test_enum_narrowing_breaks_requests_and_widening_breaks_responses() -> None:
    narrow = {"type": "string", "enum": ["a"]}
    wide = {"type": "string", "enum": ["a", "b"]}
    assert breaking_changes(doc(request=obj(k=wide)), doc(request=obj(k=narrow))) == [
        "POST /items request body.k: enum lost values ['b']"
    ]
    assert breaking_changes(doc(request=obj(k=narrow)), doc(request=obj(k=wide))) == []
    assert breaking_changes(doc(response=obj(k=narrow)), doc(response=obj(k=wide))) == [
        "POST /items response 200.k: enum gained values ['b']"
    ]


def test_a_removed_success_status_breaks_clients() -> None:
    old = doc()
    new = doc()
    new["paths"]["/items"]["post"]["responses"] = {"201": {"description": "made"}}
    assert breaking_changes(old, new) == ["POST /items: removed response 200"]


def test_new_required_parameters_break_clients() -> None:
    old = doc(parameters=[])
    new = doc(
        parameters=[
            {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}}
        ]
    )
    assert breaking_changes(old, new) == [
        "POST /items: new required query parameter 'q'"
    ]


def test_references_are_followed_and_cycles_terminate() -> None:
    def with_components(properties: dict[str, Any]) -> dict[str, Any]:
        document = doc(response={"$ref": "#/components/schemas/Node"})
        document["components"] = {
            "schemas": {
                "Node": obj(child={"$ref": "#/components/schemas/Node"}, **properties)
            }
        }
        return document

    old = with_components({"id": {"type": "integer"}})
    new = with_components({})
    assert breaking_changes(old, new) == [
        "POST /items response 200: removed property 'id'"
    ]


def test_cli_reports_breaking_changes_with_exit_code_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = tmp_path / "baseline.json"
    assert main(["openapi", TARGET, "--output", str(baseline)]) == 0
    capsys.readouterr()
    assert main(["openapi", TARGET, "--breaking-against", str(baseline)]) == 0
    assert capsys.readouterr().out == ""

    data = json.loads(baseline.read_text())
    data["paths"]["/gone"] = {"get": {"responses": {"200": {"description": "ok"}}}}
    baseline.write_text(json.dumps(data), encoding="utf-8")
    assert main(["openapi", TARGET, "--breaking-against", str(baseline)]) == 1
    assert "removed operation GET /gone" in capsys.readouterr().err


def test_cli_rejects_a_missing_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope.json"
    assert main(["openapi", TARGET, "--breaking-against", str(missing)]) == 2
    assert "does not exist" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("content", "message"),
    [("{", "cannot read JSON"), ("[]", "is not an OpenAPI object")],
)
def test_cli_rejects_an_unreadable_or_non_object_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str, message: str
) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(content, encoding="utf-8")
    assert main(["openapi", TARGET, "--breaking-against", str(baseline)]) == 2
    assert message in capsys.readouterr().err


def param(required: bool = False, **schema: Any) -> dict[str, Any]:
    return {
        "name": "q",
        "in": "query",
        "required": required,
        "schema": schema or {"type": "string"},
    }


def test_a_parameter_that_becomes_required_or_changes_type_breaks_clients() -> None:
    old = doc(parameters=[param(), param(enum=["a", "b"])])
    old["paths"]["/items"]["post"]["parameters"][1]["name"] = "kind"
    assert breaking_changes(old, old) == []
    new = doc(parameters=[param(required=True, type="integer"), param(enum=["a"])])
    new["paths"]["/items"]["post"]["parameters"][1]["name"] = "kind"
    assert breaking_changes(old, new) == [
        "POST /items: query parameter 'q' became required",
        "POST /items: query parameter 'q': type changed from string to integer",
        "POST /items: query parameter 'kind': enum lost values ['b']",
    ]


def test_a_new_required_body_or_a_dropped_response_body_breaks_clients() -> None:
    old = doc()
    new = doc(request=obj())
    assert breaking_changes(old, new) == []
    new["paths"]["/items"]["post"]["requestBody"]["required"] = True
    assert breaking_changes(old, new) == ["POST /items: new required request body"]
    dropped = doc()
    dropped["paths"]["/items"]["post"]["responses"]["200"] = {"description": "ok"}
    assert breaking_changes(old, dropped) == [
        "POST /items: response 200 dropped its JSON body"
    ]
