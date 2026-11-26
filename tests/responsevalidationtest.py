"""Responses are validated by default: a return value that breaks its annotation is
a 500, and an annotation that cannot be checked stops the app at startup."""

import logging
import sys
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


class Plain:
    """Not a Schema, a Model or a scalar: nothing to check it against."""


def note() -> NoteOut:
    return NoteOut(id=1, title="ok")


@http.get("/good")
async def good() -> NoteOut:
    return note()


@http.get("/dict")
async def as_dict() -> NoteOut:
    return {"id": 1, "title": SECRET}  # type: ignore[return-value]


@http.get("/unchecked", validate_response=False)
async def unchecked() -> NoteOut:
    return {"id": 1, "title": "plain"}  # type: ignore[return-value]


@http.get("/model")
async def raw_model() -> NoteOut:
    return Row(id=2, title=SECRET)  # type: ignore[return-value]


@http.get("/from-model")
async def from_model() -> NoteOut:
    return NoteOut.from_model(Row(id=2, title="from a model"))


@http.get("/optional/{found}")
async def optional(found: bool) -> NoteOut | None:
    return note() if found else None


@http.get("/missing")
async def missing() -> NoteOut:
    return None  # type: ignore[return-value]


@http.get("/many")
async def many() -> list[NoteOut]:
    return [note(), note()]


@http.get("/many-bad")
async def many_bad() -> list[NoteOut]:
    return [note(), {"id": 2, "title": SECRET}]  # type: ignore[list-item]


@http.get("/page")
async def paged() -> Page[NoteOut]:
    return Page(items=[note()], page=1, page_size=10, total=1)


@http.get("/count")
async def count() -> int:
    return True


@http.get("/escape/{use}")
async def escape(use: bool) -> NoteOut | Response:
    return Response(202, b"{}") if use else note()


@http.get("/gone", status=204)
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
        raw_model,
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


async def test_valid_results_pass_and_the_opt_out_skips_the_check(
    every_route: Hinoki,
) -> None:
    async with ASGIClient(every_route) as client:
        assert (await client.get("/good")).body == {"id": 1, "title": "ok"}
        assert (await client.get("/unchecked")).status == 200
        assert (await client.get("/from-model")).body == {
            "id": 2,
            "title": "from a model",
        }
        assert (await client.get("/optional/true")).status == 200
        assert (await client.get("/optional/false")).status == 204
        assert len((await client.get("/many")).body) == 2
        assert (await client.get("/page")).body["total"] == 1
        assert (await client.get("/escape/true")).status == 202
        assert (await client.get("/escape/false")).status == 200
        assert (await client.get("/gone")).status == 204


@pytest.mark.parametrize("path", ["/dict", "/model", "/missing", "/many-bad", "/count"])
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
    # in development the answer says which action broke which annotation
    assert "as_dict returned a dict" in str(reply.body)
    assert "return annotation NoteOut" in str(reply.body)


@pytest.mark.parametrize(
    ("handler", "message"),
    [
        ("untyped", "must name a concrete type"),
        ("anything", "must name a concrete type"),
        ("an_object", "must name a concrete type"),
        ("a_set", "cannot be checked"),
        ("a_class", "cannot be checked"),
        ("nested_response", "only be returned whole"),
    ],
)
def test_annotations_that_cannot_be_checked_stop_the_app_at_startup(
    handler: str, message: str
) -> None:
    @http.get("/untyped")
    async def untyped():  # type: ignore[no-untyped-def]
        return 1

    @http.get("/anything")
    async def anything() -> Any:
        return 1

    @http.get("/object")
    async def an_object() -> object:
        return 1

    @http.get("/set")
    async def a_set() -> set[int]:
        return set()

    @http.get("/class")
    async def a_class() -> Plain:
        return Plain()

    @http.get("/nested")
    async def nested_response() -> list[NoteOut | Response]:
        return []

    chosen = {
        "untyped": untyped,
        "anything": anything,
        "an_object": an_object,
        "a_set": a_set,
        "a_class": a_class,
        "nested_response": nested_response,
    }[handler]
    with pytest.raises(RegistrationError, match=message) as raised:
        Hinoki(include=[chosen]).asgi()

    assert "validate_response=False" in str(raised.value)  # the way out is named


async def test_the_opt_out_lets_an_unchecked_annotation_start() -> None:
    @http.get("/free", validate_response=False)
    async def free() -> set[int]:
        return {1}

    Hinoki(include=[free]).asgi()  # no RegistrationError


async def test_response_only_none_and_collections_of_anything_are_checkable() -> None:
    @http.get("/r")
    async def only_response() -> Response:
        return Response(202, b"{}")

    @http.get("/n", status=202)
    async def nothing() -> None:
        return None

    @http.get("/d")
    async def mapping() -> dict[str, Any]:
        return {"a": 1}

    @http.get("/m")
    async def typed_mapping() -> dict[str, int]:
        return {"a": "no"}  # type: ignore[dict-item]

    async with ASGIClient(
        Hinoki(include=[only_response, nothing, mapping, typed_mapping])
    ) as client:
        assert (await client.get("/r")).status == 202
        assert (await client.get("/n")).status == 202
        assert (await client.get("/d")).body == {"a": 1}
        assert (await client.get("/m")).status == 500


async def test_a_route_group_validates_by_default_too() -> None:
    group = RouteGroup("/g")

    @group.get("/bad")
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

    @http.post("/rows", atomic=True)
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


def test_a_single_arm_is_checked_directly_and_a_union_by_any_arm() -> None:
    single = compile_response_check("action", NoteOut)
    assert single.__qualname__.startswith("class_check")
    assert single(note())
    assert not single(None)

    union = compile_response_check("action", NoteOut | None)
    assert union(note())
    assert union(None)
    assert not union(3)


def test_a_union_check_creates_no_generator_per_response() -> None:
    union = compile_response_check("action", NoteOut | None)
    sample = note()  # built before profiling: a first construction is not the check
    created: list[str] = []

    def watch(frame: Any, event: str, _arg: object) -> None:
        if event == "call":
            created.append(frame.f_code.co_name)

    sys.setprofile(watch)
    try:
        assert union(sample)
        assert union(None)
        assert not union(3)
    finally:
        sys.setprofile(None)

    assert "<genexpr>" not in created


@pytest.mark.parametrize("native", [False, True])
def test_object_dict_list_preserves_subclass_iteration_and_mutation(
    native: bool,
) -> None:
    events: list[str] = []
    values: list[object] = []

    class Special(dict[str, object]):
        def items(self) -> Any:
            events.append("items")
            values.append({"added": object()})
            return super().items()

    values.extend([{"first": 1}, Special(x=1)])

    def item(value: object) -> bool:
        return isinstance(value, dict) and all(
            type(key) is str for key, _ in value.items()
        )

    def reference(value: object) -> bool:
        return isinstance(value, list) and all(item(entry) for entry in value)

    check = (
        compile_response_check("action", list[dict[str, object]])
        if native
        else reference
    )
    assert check(values)
    assert events == ["items"]
    assert len(values) == 3


def test_object_dict_list_matches_python_for_subclasses_and_errors() -> None:
    events: list[str] = []

    class Key(str):
        pass

    class CustomDict(dict[str, object]):
        def items(self) -> Any:
            events.append("dict")
            raise RuntimeError("items called")

    class CustomList(list[object]):
        def __iter__(self) -> Any:
            events.append("list")
            return iter([{"ok": 1}])

    check = compile_response_check("action", list[dict[str, object]])
    assert check([])
    assert check([{"ok": object()}])
    assert not check([{Key("bad"): 1}])
    assert not check([{}, 1])
    assert not check([{1: "bad"}, CustomDict()])
    assert events == []
    assert check(CustomList([None]))
    assert events == ["list"]
    with pytest.raises(RuntimeError, match="items called"):
        check([CustomDict()])
    assert events == ["list", "dict"]


@pytest.mark.parametrize("mutation", ["remove", "append_invalid", "raise"])
def test_object_dict_list_matches_python_when_nested_dict_changes_list(
    mutation: str,
) -> None:
    def run(native: bool) -> tuple[object, list[str]]:
        events: list[str] = []
        values: list[object] = []

        class CustomDict(dict[str, object]):
            def items(self) -> Any:
                events.append("items")
                if mutation == "remove":
                    values.pop()
                elif mutation == "append_invalid":
                    values.append({1: "bad"})
                else:
                    raise RuntimeError("custom failure")
                return super().items()

        values.extend([CustomDict(a=1), {1: "bad"} if mutation == "remove" else {}])

        def item(value: object) -> bool:
            return isinstance(value, dict) and all(
                type(key) is str for key, _ in value.items()
            )

        def reference(value: object) -> bool:
            return isinstance(value, list) and all(item(entry) for entry in value)

        check = (
            compile_response_check("action", list[dict[str, object]])
            if native
            else reference
        )
        try:
            result: object = check(values)
        except RuntimeError as error:
            result = (type(error), str(error))
        return result, events

    assert run(True) == run(False)
