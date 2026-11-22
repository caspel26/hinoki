"""Per-route OpenAPI controls: include_in_schema, tags, deprecated, operation_id."""

from uuid import UUID, uuid4

import pytest
from openapi_spec_validator import validate

from hinoki import Access, Hinoki, RouteGroup, Schema, crud, http
from hinoki.ai.mcpapp import ExposureError, select
from hinoki.errors import RegistrationError
from hinoki.http.request import Request
from hinoki.orm import Model, field
from hinoki.testing import ASGIClient
from tests.providers import TokenAuth

editor = Access(auth=TokenAuth(), permissions=["posts:write"])
EDITOR = {"authorization": "Bearer editor-token"}
READER = {"authorization": "Bearer reader-token"}


class Secret(Schema):
    code: str


class Open(Schema):
    name: str


@http.get("/open")
def open_route(request: Request) -> Open:
    return Open(name="open")


@http.get("/internal", include_in_schema=False, access=editor)
def internal(request: Request) -> Secret:
    return Secret(code="x")


class Note(Model, table="route_control_notes"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str = field(max_length=40)


class NoteOut(Schema):
    id: UUID
    title: str


def test_excluded_route_is_missing_from_the_document() -> None:
    app = Hinoki()
    app.include(open_route, internal)
    document = app.openapi()
    validate(document)
    assert set(document["paths"]) == {"/open"}


def test_models_only_used_by_excluded_routes_are_not_components() -> None:
    app = Hinoki()
    app.include(open_route, internal)
    assert set(app.openapi()["components"]["schemas"]) == {"Open"}


def test_securitySchemes_of_excluded_routes_are_not_documented() -> None:
    app = Hinoki()
    app.include(internal)
    assert app.openapi().get("components", {}).get("securitySchemes") is None


async def test_excluded_route_is_still_served_and_protected() -> None:
    app = Hinoki()
    app.include(open_route, internal)
    async with ASGIClient(app) as client:
        assert (await client.get("/internal")).status == 401
        assert (await client.get("/internal", headers=READER)).status == 403
        reply = await client.get("/internal", headers=EDITOR)
        assert (reply.status, reply.body) == (200, {"code": "x"})


def test_tags_deprecated_and_custom_operation_id() -> None:
    @http.get("/old", tags=["legacy", "legacy", "ops"], deprecated=True)
    def old(request: Request) -> Open:
        raise NotImplementedError

    @http.get("/new", operation_id="readNew")
    def new(request: Request) -> Open:
        raise NotImplementedError

    app = Hinoki()
    app.include(old, new)
    document = app.openapi()
    validate(document)
    assert document["paths"]["/old"]["get"]["tags"] == ["legacy", "ops"]
    assert document["paths"]["/old"]["get"]["deprecated"] is True
    assert document["paths"]["/new"]["get"]["operationId"] == "readNew"
    assert "tags" not in document["paths"]["/new"]["get"]
    assert "deprecated" not in document["paths"]["/new"]["get"]


def test_generated_operation_id_never_collides_with_a_custom_one() -> None:
    @http.get("/a", operation_id="b_route")
    def a_route(request: Request) -> Open:
        raise NotImplementedError

    @http.get("/b")
    def b_route(request: Request) -> Open:
        raise NotImplementedError

    app = Hinoki()
    app.include(a_route, b_route)
    paths = app.openapi()["paths"]
    assert paths["/a"]["get"]["operationId"] == "b_route"
    assert paths["/b"]["get"]["operationId"] != "b_route"


def test_duplicate_operation_ids_stop_registration() -> None:
    @http.get("/a", operation_id="same")
    def first(request: Request) -> Open:
        raise NotImplementedError

    @http.get("/b", operation_id="same")
    def second(request: Request) -> Open:
        raise NotImplementedError

    app = Hinoki()
    with pytest.raises(RegistrationError, match="operation_id 'same'"):
        app.include(first, second)


def test_duplicate_operation_id_with_an_excluded_route_is_allowed() -> None:
    @http.get("/a", operation_id="same")
    def first(request: Request) -> Open:
        raise NotImplementedError

    @http.get("/b", operation_id="same", include_in_schema=False)
    def second(request: Request) -> Open:
        raise NotImplementedError

    app = Hinoki()
    app.include(first, second)
    assert set(app.openapi()["paths"]) == {"/a"}


@pytest.mark.parametrize(
    "options",
    [
        {"include_in_schema": "no"},
        {"tags": "legacy"},
        {"tags": [""]},
        {"deprecated": 1},
        {"operation_id": " "},
    ],
)
def test_invalid_options_fail_at_declaration(options: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="takes"):
        http.get("/x", **options)(lambda: None)  # type: ignore[arg-type]


def test_route_group_default_and_override() -> None:
    group = RouteGroup("/ops", include_in_schema=False, tags=["ops"])

    @group.get("/hidden")
    def hidden(request: Request) -> Open:
        raise NotImplementedError

    @group.get("/shown", include_in_schema=True, tags=["extra"])
    def shown(request: Request) -> Open:
        raise NotImplementedError

    app = Hinoki()
    app.include(group)
    paths = app.openapi()["paths"]
    assert set(paths) == {"/ops/shown"}
    assert paths["/ops/shown"]["get"]["tags"] == ["ops", "extra"]


def test_crud_include_in_schema_per_resource_and_per_action() -> None:
    whole = crud(
        Note,
        path="/hidden-notes",
        read=NoteOut,
        delete=True,
        access=editor,
        include_in_schema=False,
        name="HiddenNotes",
    )
    partial = crud(
        Note,
        path="/notes",
        read=NoteOut,
        delete=True,
        access=editor,
        include_in_schema={"delete": False},
        tags=["notes"],
        deprecated=True,
        name="PartialNotes",
    )
    app = Hinoki()
    app.include(whole, partial)
    document = app.openapi()
    validate(document)
    assert set(document["paths"]) == {"/notes", "/notes/{item_id}"}
    assert "delete" not in document["paths"]["/notes/{item_id}"]
    for operation in (document["paths"]["/notes"]["get"],):
        assert operation["tags"] == ["notes"]
        assert operation["deprecated"] is True


def test_crud_rejects_unknown_action_names() -> None:
    with pytest.raises(RegistrationError, match="unknown actions"):
        crud(Note, path="/n", read=NoteOut, include_in_schema={"purge": False})


def test_mcp_refuses_a_route_left_out_of_the_document() -> None:
    app = Hinoki()
    app.include(open_route, internal)
    with pytest.raises(ExposureError, match="include_in_schema=False"):
        select(app, ["*internal"], allow_writes=False, mount="/mcp")
    assert [t.route.name for t in select(app, ["*open_route"], False, "/mcp")] == [
        "open_route"
    ]
