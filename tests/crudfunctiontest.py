"""``crud(...)``: the function and class-decorator form of generated CRUD."""

import os
from functools import partial
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from hinoki import (
    API,
    UNSET,
    Access,
    Actions,
    BearerAuth,
    Caller,
    CrudAccess,
    Hinoki,
    Mount,
    Schema,
    Unset,
    crud,
    http,
)
from hinoki.db import Database, DatabaseModule
from hinoki.errors import RegistrationError
from hinoki.http.request import Request
from hinoki.orm import Model, QuerySet, field
from hinoki.rules import Project, RuleSet, qualified
from tests.harness import Pool

# Fixtures declare owner= resources on purpose: the owner read-scope warning is
# not what these tests check.
OWNER_READS = r"\w+. owner=.* constrains writes only"
pytestmark = pytest.mark.filterwarnings(f"ignore:{OWNER_READS}:UserWarning")

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
needs_database = pytest.mark.skipif(
    URL is None, reason="HINOKI_TEST_DATABASE_URL is not set"
)
ANN = {"authorization": "Bearer ann"}
BOB = {"authorization": "Bearer bob"}
JSON = "application/json"


class Note(Model, table="crud_function_notes"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    owner: str = field(max_length=20, default="")
    title: str = field(max_length=40)
    body: str = ""


class NoteOut(Schema):
    id: UUID
    owner: str
    title: str
    body: str


class NoteIn(Schema):
    title: str
    body: str = ""


class NotePatch(Schema):
    title: str | Unset = UNSET
    body: str | Unset = UNSET


class Tokens(BearerAuth[str]):
    async def resolve_token(self, token: str) -> Caller[str] | None:
        name = token.strip()
        scopes = {"note:write"} | ({"note:admin"} if name == "admin" else set())
        return Caller(id=name, user=name, permissions=frozenset(scopes))


auth = Tokens()
writer = Access(auth=auth, permissions=["note:write"])
admin = Access(auth=auth, permissions=["note:admin"])


def shape(api: Any) -> dict[tuple[str, str], Access | None]:
    return {(route.method, route.path): route.access for route in api.routes()}


def simple(**changes: Any) -> Any:
    options: dict[str, Any] = {"path": "/notes", "read": NoteOut, "access": writer}
    options.update(changes)
    return crud(Note, **options).api()


def test_function_form_generates_the_requested_actions() -> None:
    api = simple(create=NoteIn, update=NotePatch, delete=True, owner="owner")
    assert set(shape(api)) == {
        ("GET", "/notes"),
        ("GET", "/notes/{item_id}"),
        ("POST", "/notes"),
        ("PATCH", "/notes/{item_id}"),
        ("DELETE", "/notes/{item_id}"),
    }


def test_a_single_policy_protects_every_action_including_reads() -> None:
    access = shape(simple(create=NoteIn, delete=True))
    assert set(access.values()) == {writer}


def test_crud_access_sets_each_action_and_falls_back_to_default() -> None:
    api = simple(
        create=NoteIn,
        update=NotePatch,
        delete=True,
        access=CrudAccess(default=writer, list=None, delete=admin),
    )
    access = shape(api)
    assert access[("GET", "/notes")] is None
    assert access[("GET", "/notes/{item_id}")] is writer
    assert access[("POST", "/notes")] is writer
    assert access[("PATCH", "/notes/{item_id}")] is writer
    assert access[("DELETE", "/notes/{item_id}")] is admin


def test_crud_access_rejects_unknown_actions() -> None:
    with pytest.raises(TypeError):
        CrudAccess(publish=writer)  # type: ignore[call-arg]


def test_writes_without_a_policy_fail_at_declaration() -> None:
    with pytest.raises(RegistrationError, match="need access"):
        simple(access=None, create=NoteIn)
    with pytest.raises(RegistrationError, match="need access"):
        simple(access=CrudAccess(list=writer), delete=True)


def test_public_writes_must_be_explicit() -> None:
    api = simple(access=None, create=NoteIn, public_writes=True)
    assert shape(api)[("POST", "/notes")] is None


def test_rules_scoped_to_the_declaring_module_see_the_function_form() -> None:
    declared = crud(
        Note,
        path="/notes",
        read=NoteOut,
        create=NoteIn,
        access=CrudAccess(default=writer, list=None),
    )
    app = Hinoki(include=[declared], enforce_rules=False)
    assert f"{__name__}.NoteCrud.list" in {qualified(r) for r in app.routes}
    found = RuleSet(Actions.require_access(match=f"{__name__}.*")).run(Project(app))
    assert [d.subject for d in found] == [f"{__name__}.NoteCrud.list"]


def test_a_helper_called_at_import_names_actions_after_the_feature_module() -> None:
    from tests.projects import crudnotes  # noqa: PLC0415 - imported here to run its top level

    app = Hinoki(include=[crudnotes.memos], enforce_rules=False)
    names = {qualified(route) for route in app.routes}
    assert names == {
        "tests.projects.crudnotes.MemoCrud.list",
        "tests.projects.crudnotes.MemoCrud.retrieve",
    }
    found = RuleSet(Actions.require_access(match="tests.projects.crudnotes.*"))
    assert len(found.run(Project(app))) == 2


def test_name_sets_the_class_and_optionally_the_module() -> None:
    options: dict[str, Any] = {"path": "/notes", "read": NoteOut, "access": writer}
    short = crud(Note, name="ArchiveCrud", **options).api()
    assert (short.__module__, short.__name__) == (__name__, "ArchiveCrud")
    full = crud(Note, name="notes.apps.archive.Archive", **options).api()
    assert (full.__module__, full.__name__) == ("notes.apps.archive", "Archive")
    for bad in ("", "1Note", "notes..Archive", "notes.Archive-2"):
        with pytest.raises(RegistrationError, match="name="):
            crud(Note, name=bad, **options)
    with pytest.raises(RegistrationError, match="name="):

        @crud(Note, name="Other", **options)
        class Notes(API):
            pass


def test_two_crud_values_with_one_name_are_refused_at_include() -> None:
    options: dict[str, Any] = {"read": NoteOut, "access": writer}
    notes = crud(Note, path="/notes", **options)
    archive = crud(Note, path="/archive", **options)
    app = Hinoki(enforce_rules=False)
    with pytest.raises(RegistrationError, match=r"NoteCrud\.list.*name="):
        app.include(notes, archive)
    assert app.routes == ()
    app.include(notes, crud(Note, path="/archive", name="ArchiveCrud", **options))
    assert len(app.routes) == 4


def test_one_crud_value_mounted_twice_keeps_one_name() -> None:
    notes = crud(Note, path="/notes", read=NoteOut, access=writer)
    app = Hinoki(include=[Mount("/a", notes), Mount("/b", notes)], enforce_rules=False)
    assert len(app.routes) == 4
    assert {qualified(route) for route in app.routes} == {
        f"{__name__}.NoteCrud.list",
        f"{__name__}.NoteCrud.retrieve",
    }


def test_omit_leaves_an_action_to_handwritten_code() -> None:
    @crud(Note, path="/notes", read=NoteOut, access=writer, omit=("list",))
    class Notes(API):
        @http.get("/", access=writer)
        async def listing(self, request: Request) -> list[NoteOut]:
            return []

    assert Notes.__name__ == "Notes"
    names = {route.handler.__name__ for route in Notes.routes()}
    assert names == {"listing", "retrieve"}


def test_decorator_keeps_hooks_and_handwritten_actions() -> None:
    @crud(Note, path="/notes", read=NoteOut, access=writer)
    class Notes(API):
        def get_queryset(self, principal: Caller[Any] | None) -> QuerySet[Note]:
            return Note.all()

        @http.get("/health", access=None)
        async def health(self, request: Request) -> dict[str, str]:
            return {"status": "ok"}

    assert ("GET", "/notes/health") in shape(Notes)
    assert ("GET", "/notes") in shape(Notes)


def test_a_generated_action_cannot_also_be_handwritten() -> None:
    with pytest.raises(RegistrationError, match="both generated and handwritten"):

        @crud(Note, path="/notes", read=NoteOut, access=writer)
        class Notes(API):
            @http.get("/")
            async def list(self, request: Request) -> list[NoteOut]:
                return []


def test_the_class_form_accepts_the_write_hooks() -> None:
    @crud(Note, path="/notes", read=NoteOut, access=writer, create=NoteIn)
    class Notes(API):
        async def store(self, values: dict[str, Any], principal: Any) -> Any:
            return await Note.acreate(**values)

        async def change(
            self, item: Any, scoped: Any, values: dict[str, Any], principal: Any
        ) -> Any:
            return item

        async def remove(self, item: Any, scoped: Any, principal: Any) -> bool:
            return True

        def written_queryset(self, principal: Any) -> QuerySet[Note]:
            return Note.all()

    assert ("POST", "/notes") in shape(Notes)


def test_an_internal_helper_is_not_a_hook() -> None:
    with pytest.raises(RegistrationError, match="reload_written"):

        @crud(Note, path="/notes", read=NoteOut, access=writer)
        class Notes(API):
            async def reload_written(self, item_id: Any, principal: Any) -> Any:
                return None


def test_functools_partial_shares_options_between_declarations() -> None:
    staff = partial(crud, access=writer, read=NoteOut)
    one = staff(Note, path="/one").api()
    two = staff(Note, path="/two", create=NoteIn).api()
    assert shape(one)[("GET", "/one")] is writer
    assert ("POST", "/two") in shape(two)
    assert ("POST", "/one") not in shape(one)


def test_a_crud_class_must_extend_api_only() -> None:
    with pytest.raises(RegistrationError, match="exactly one base, API"):

        @crud(Note, path="/notes", read=NoteOut, access=writer)
        class Notes:  # type: ignore[type-var]
            pass


def test_declarations_are_checked_like_the_class_form() -> None:
    with pytest.raises(RegistrationError, match="no column 'nope'"):
        simple(create=NoteIn, owner="nope")


def test_super_works_inside_a_decorated_class() -> None:
    @crud(Note, path="/notes", read=NoteOut, access=writer)
    class Notes(API):
        def get_queryset(self, principal: Caller[Any] | None) -> QuerySet[Note]:
            return cast(QuerySet[Note], super().get_queryset(principal))  # type: ignore[misc]

    assert Notes().get_queryset(None) is not None


@needs_database
async def test_function_form_runs_against_a_database(pool: Pool) -> None:
    assert URL is not None

    @crud(
        Note,
        path="/notes",
        read=NoteOut,
        create=NoteIn,
        delete=True,
        owner="owner",
        identity_key=str,
        access=writer,
    )
    class Notes(API):
        def get_queryset(self, principal: Caller[Any] | None) -> QuerySet[Note]:
            assert principal is not None
            return Note.filter(owner=principal.user)

    admin = await Database.connect(URL, max_size=1)
    await admin.execute("DROP TABLE IF EXISTS crud_function_notes")
    await admin.execute(
        "CREATE TABLE crud_function_notes (id uuid PRIMARY KEY, "
        "owner varchar(20) NOT NULL, title varchar(40) NOT NULL, body text NOT NULL)"
    )
    app = Hinoki(title="Notes")
    app.include(Notes)
    app.install(DatabaseModule(URL, max_size=3))
    client = await pool.start(app)
    try:
        send = client.request
        created = await send(
            "POST", "/notes", b'{"title": "mine"}', content_type=JSON, headers=ANN
        )
        assert created.status == 201
        path = f"/notes/{created.body['id']}"
        assert (await send("GET", path, headers=ANN)).status == 200
        assert (await send("GET", path, headers=BOB)).status == 404
        assert (await send("DELETE", path, headers=BOB)).status == 404
        assert (await send("DELETE", path, headers=ANN)).status == 204
    finally:
        await admin.execute("DROP TABLE IF EXISTS crud_function_notes")
        admin.close()


@needs_database
async def test_write_hooks_in_a_decorated_class_run_on_a_database(pool: Pool) -> None:
    assert URL is not None
    calls: list[str] = []

    @crud(
        Note,
        path="/notes",
        read=NoteOut,
        create=NoteIn,
        update=NotePatch,
        delete=True,
        owner="owner",
        identity_key=str,
        access=writer,
    )
    class Notes(API):
        async def store(self, values: dict[str, Any], principal: Any) -> Any:
            calls.append("store")
            values["title"] = str(values["title"]).upper()
            return await Note.acreate(**values)

        async def change(
            self, item: Any, scoped: Any, values: dict[str, Any], principal: Any
        ) -> Any:
            calls.append("change")
            return await super().change(item, scoped, values, principal)  # type: ignore[misc]

        async def remove(self, item: Any, scoped: Any, principal: Any) -> bool:
            calls.append("remove")
            return cast(bool, await super().remove(item, scoped, principal))  # type: ignore[misc]

    admin = await Database.connect(URL, max_size=1)
    await admin.execute("DROP TABLE IF EXISTS crud_function_notes")
    await admin.execute(
        "CREATE TABLE crud_function_notes (id uuid PRIMARY KEY, "
        "owner varchar(20) NOT NULL, title varchar(40) NOT NULL, body text NOT NULL)"
    )
    app = Hinoki(title="Notes")
    app.include(Notes)
    app.install(DatabaseModule(URL, max_size=3))
    client = await pool.start(app)
    try:
        send = client.request
        created = await send(
            "POST", "/notes", b'{"title": "mine"}', content_type=JSON, headers=ANN
        )
        assert created.status == 201
        assert created.body["title"] == "MINE"
        path = f"/notes/{created.body['id']}"
        patched = await send(
            "PATCH", path, b'{"body": "hi"}', content_type=JSON, headers=ANN
        )
        assert patched.status == 200
        assert (await send("DELETE", path, headers=BOB)).status == 403
        assert (await send("DELETE", path, headers=ANN)).status == 204
        assert calls == ["store", "change", "remove"]
    finally:
        await admin.execute("DROP TABLE IF EXISTS crud_function_notes")
        admin.close()
