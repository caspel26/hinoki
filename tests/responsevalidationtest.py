"""``validate_response=True``: a return value that breaks its annotation is a 500."""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import pytest

from hinoki import Hinoki, Schema, http
from hinoki.db import Database, DatabaseModule
from hinoki.errors import RegistrationError
from hinoki.http.response import Response
from hinoki.orm import Model, pk, use_database
from hinoki.pagination import Page
from hinoki.routing import RouteGroup
from hinoki.runtime.responsecheck import compile_response_check
from hinoki.settings import AppSettings
from hinoki.testing import ASGIClient

SECRET = "do-not-leak-this-value"  # noqa: S105 - a marker, not a credential


class NoteOut(Schema):
    id: int
    title: str


class Row(Model, table="validated_rows"):
    id: int = pk()
    title: str


def note() -> NoteOut:
    return NoteOut(id=1, title="ok")


@http.get("/good", validate_response=True)
async def good() -> NoteOut:
    return note()


@http.get("/dict", validate_response=True)
async def as_dict() -> NoteOut:
    return {"id": 1, "title": SECRET}  # type: ignore[return-value]


@http.get("/unchecked")
async def unchecked() -> NoteOut:
    return {"id": 1, "title": "plain"}  # type: ignore[return-value]


@http.get("/model", validate_response=True)
async def from_model() -> NoteOut:
    return Row(id=2, title="from a model")  # type: ignore[return-value]


@http.get("/optional/{found}", validate_response=True)
async def optional(found: bool) -> NoteOut | None:
    return note() if found else None


@http.get("/missing", validate_response=True)
async def missing() -> NoteOut:
    return None  # type: ignore[return-value]


@http.get("/many", validate_response=True)
async def many() -> list[NoteOut]:
    return [note(), note()]


@http.get("/many-bad", validate_response=True)
async def many_bad() -> list[NoteOut]:
    return [note(), {"id": 2, "title": SECRET}]  # type: ignore[list-item]


@http.get("/page", validate_response=True)
async def paged() -> Page[NoteOut]:
    return Page(items=[note()], page=1, page_size=10, total=1)


@http.get("/count", validate_response=True)
async def count() -> int:
    return True


@http.get("/escape/{use}", validate_response=True)
async def escape(use: bool) -> NoteOut | Response:
    return Response(202, b"{}") if use else note()


@http.get("/gone", status=204, validate_response=True)
async def gone() -> NoteOut:
    return None  # type: ignore[return-value]


def app_of(*routes: Callable[..., Any]) -> Hinoki:
    return Hinoki(include=list(routes))


@pytest.fixture
def every_route() -> Hinoki:
    return app_of(
        good,
        as_dict,
        unchecked,
        from_model,
        optional,
        missing,
        many,
        many_bad,
        paged,
        count,
        escape,
        gone,
    )


async def test_valid_results_pass_and_the_option_is_off_by_default(
    every_route: Hinoki,
) -> None:
    async with ASGIClient(every_route) as client:
        assert (await client.get("/good")).body == {"id": 1, "title": "ok"}
        assert (await client.get("/unchecked")).status == 200
        assert (await client.get("/model")).body == {"id": 2, "title": "from a model"}
        assert (await client.get("/optional/true")).status == 200
        assert (await client.get("/optional/false")).status == 204
        assert len((await client.get("/many")).body) == 2
        assert (await client.get("/page")).body["total"] == 1
        assert (await client.get("/escape/true")).status == 202
        assert (await client.get("/escape/false")).status == 200
        assert (await client.get("/gone")).status == 204


@pytest.mark.parametrize("path", ["/dict", "/missing", "/many-bad", "/count"])
async def test_a_mismatch_is_a_value_free_500(
    every_route: Hinoki, path: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        async with ASGIClient(every_route) as client:
            reply = await client.get(path)

    assert reply.status == 500
    assert reply.body["error"] == "internal_error"
    assert SECRET not in str(reply.body)
    assert SECRET not in caplog.text


async def test_debug_mode_still_names_no_value() -> None:
    app = Hinoki(settings=AppSettings(debug=True), include=[as_dict])
    async with ASGIClient(app) as client:
        reply = await client.get("/dict")

    assert reply.status == 500
    assert SECRET not in str(reply.body)


@pytest.mark.parametrize(
    ("handler", "message"),
    [
        ("untyped", "needs a concrete return annotation"),
        ("anything", "needs a concrete return annotation"),
        ("only_response", "only returns Response"),
        ("a_dict", "cannot check the return type"),
        ("nested_response", "only be returned whole"),
    ],
)
def test_unsupported_annotations_fail_at_registration(
    handler: str, message: str
) -> None:
    @http.get("/untyped", validate_response=True)
    async def untyped():  # type: ignore[no-untyped-def]
        return 1

    @http.get("/anything", validate_response=True)
    async def anything() -> Any:
        return 1

    @http.get("/only", validate_response=True)
    async def only_response() -> Response:
        return Response(200, b"")

    @http.get("/dict", validate_response=True)
    async def a_dict() -> dict[str, int]:
        return {}

    @http.get("/nested", validate_response=True)
    async def nested_response() -> list[NoteOut | Response]:
        return []

    chosen = {
        "untyped": untyped,
        "anything": anything,
        "only_response": only_response,
        "a_dict": a_dict,
        "nested_response": nested_response,
    }[handler]
    with pytest.raises(RegistrationError, match=message):
        Hinoki(include=[chosen]).asgi()


async def test_a_route_group_passes_the_option_through() -> None:
    group = RouteGroup("/g")

    @group.get("/bad", validate_response=True)
    async def bad() -> NoteOut:
        return {"id": 1, "title": "x"}  # type: ignore[return-value]

    async with ASGIClient(Hinoki(include=[group])) as client:
        assert (await client.get("/g/bad")).status == 500


async def test_a_mismatch_in_a_transaction_rolls_the_write_back(
    tmp_path: Path,
) -> None:
    url = f"sqlite://{tmp_path / 'validated.sqlite3'}"
    database = await Database.connect(url, max_size=1)
    await database.execute(
        "CREATE TABLE validated_rows (id integer PRIMARY KEY, title text NOT NULL)"
    )

    @http.post("/rows", transaction=True, validate_response=True)
    async def create() -> NoteOut:
        await Row.acreate(id=1, title="written")
        return {"id": 1, "title": "written"}  # type: ignore[return-value]

    app = Hinoki(include=[create], modules=[DatabaseModule(url, max_size=2)])
    async with ASGIClient(app) as client:
        assert (await client.post("/rows")).status == 500
    with use_database(database):
        assert await Row.all().acount() == 0
    database.close()


def test_annotated_return_types_are_unwrapped() -> None:
    check = compile_response_check("action", Annotated[NoteOut, "docs"])

    assert check(note())
    assert not check({"id": 1, "title": "x"})
