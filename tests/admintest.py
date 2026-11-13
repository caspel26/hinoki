"""The admin extra: templates, themes, forms, CSRF and the pages (PostgreSQL)."""

import datetime
import os
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from openapi_spec_validator import validate

from hinoki import Access, Caller, HinokiAPI, RegistrationError
from hinoki.admin import THEMES, Admin, Badge, Fieldset, ModelAdmin, Tab, Theme, csrf
from hinoki.admin.components import icon_name, initials, page_window, sparkline
from hinoki.admin.forms import FieldProblem, collect, convert, fields_for, label_of
from hinoki.admin.templates import Raw, TemplateError, Templates, escape
from hinoki.db import Database, DatabaseModule
from hinoki.orm import Model, current_database, field, foreign_key, use_database
from tests.harness import Client, Pool
from tests.providers import TokenAuth

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
needs_database = pytest.mark.skipif(
    URL is None, reason="HINOKI_TEST_DATABASE_URL is not set"
)
STAFF = {"authorization": "Bearer editor-token"}
FORM = "application/x-www-form-urlencoded"
SECRET = "s" * 40
staff = Access(auth=TokenAuth(), permissions=["posts:write"])


class Owner(Model, table="admin_owners"):
    id: int = field(primary_key=True)
    name: str = field(max_length=20)


class Note(Model, table="admin_notes"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str = field(max_length=20, unique=True)
    body: str = ""
    done: bool = False
    rank: int | None = None
    due: datetime.datetime | None = None
    owner_id: int | None = foreign_key(Owner, on_delete="set_null", default=None)


class NoteAdmin(ModelAdmin[Note]):
    label = "Notes"
    columns = ("title", "done", "rank")
    search = ("title",)
    per_page = 2
    bulk_delete = True
    show_count = True
    badges: ClassVar[dict[str, Any]] = {
        "done": {True: Badge("Done", "ok"), False: Badge("Open", "warn", "pencil")}
    }
    tabs = (Tab("Done", {"done": True}), Tab("Open", {"done": False}))
    fieldsets = (Fieldset("Organization", ("owner_id",)),)
    modified_field = "due"


class Secret(Model, table="admin_secrets"):
    id: int = field(primary_key=True)
    label: str = field(max_length=20)
    hidden: bool = False


class SecretAdmin(ModelAdmin[Secret]):
    saved: ClassVar[list[dict[str, Any]]] = []

    def queryset(self, principal: Caller[Any]) -> Any:
        return Secret.filter(hidden=False)

    def can(self, principal: Caller[Any], action: str, row: Any = None) -> bool:
        return action != "delete"

    async def save(
        self, values: dict[str, Any], principal: Caller[Any], row: Any = None
    ) -> Any:
        self.saved.append(values)
        return await super().save(values, principal, row)


def build(theme: str = "studio", templates: str | None = None) -> HinokiAPI:
    admin = Admin(
        access=staff, secret=SECRET, theme=theme, templates=templates, site="Test Admin"
    )
    admin.register(Note, NoteAdmin, slug="notes")
    admin.register(Secret, SecretAdmin, slug="secrets")
    admin.register(Owner, slug="owners")
    app = HinokiAPI(openapi_url="/openapi.json")
    app.include(admin.api())
    if URL:
        app.install(DatabaseModule(URL, max_size=3))
    return app


# --- no database ---------------------------------------------------------


def test_templates_escape_by_default_and_guard_raw_values() -> None:
    templates = Templates()
    html = templates.render(
        "notice", tone="danger", icon="info", role="alert", message="<b>&\"'"
    )
    assert "&lt;b&gt;&amp;&quot;&#x27;" in html and "<b>" not in html
    with pytest.raises(TemplateError, match="raw but not a Raw"):
        templates.render(
            "shell",
            edit="x",
            layout="sidebar",
            list_style="table",
            sidebar="<b>",
            topbar=Raw(""),
            header=Raw(""),
            notice=Raw(""),
            content=Raw(""),
            drawer=Raw(""),
        )
    with pytest.raises(TemplateError, match="no value for"):
        templates.render("notice", tone="x")
    with pytest.raises(TemplateError, match="unknown component"):
        templates.render("../../etc/passwd")
    assert escape("<a>") == "&lt;a&gt;"


def test_application_templates_and_css_override_the_built_in_set(
    tmp_path: Path,
) -> None:
    (tmp_path / "components").mkdir()
    (tmp_path / "components" / "empty.html").write_text("<i>{{ message }}</i>")
    (tmp_path / "admin.css").write_text("/* mine */")
    templates = Templates(tmp_path, "paper")
    assert templates.render("empty", message="x") == "<i>x</i>"
    assert "hn-table" in templates.sources["table"]  # the rest stays built in
    css = templates.stylesheet().decode()
    assert css.index("--hn-accent") < css.index("/* mine */")  # the app css wins last
    assert 'data-theme="paper"' in css and "prefers-color-scheme: dark" in css


def test_three_themes_share_the_component_set_and_validate() -> None:
    assert set(THEMES) == {"studio", "console", "paper"}
    assert {t.edit for t in THEMES.values()} == {"drawer", "page"}
    for name in THEMES:
        css = Templates(theme=name).stylesheet().decode()
        assert f':root[data-theme="{name}"]' in css and "--hn-accent:" in css
    for theme in THEMES.values():  # every style ships both schemes, system-driven
        css = Templates(theme=theme.name).stylesheet().decode()
        assert "prefers-color-scheme: dark" in css
        assert "[data-mode=dark]" in css and "[data-mode=light]" in css
    assert {t.layout for t in THEMES.values()} == {"sidebar", "topbar", "index"}
    assert {t.list_style for t in THEMES.values()} == {"table", "rows"}
    with pytest.raises(ValueError, match="unknown theme"):
        Templates(theme="nope")
    broken = Theme("mine", "Mine", "page", {"bg": "#fff"}, {}, {})
    with pytest.raises(ValueError, match="lacks"):
        Templates(theme=broken)
    custom = Theme(
        "mine",
        "Mine",
        "page",
        THEMES["paper"].light,
        THEMES["paper"].dark,
        THEMES["paper"].fixed,
    )
    assert 'data-theme="mine"' in Templates(theme=custom).stylesheet().decode()


def test_csrf_tokens_are_signed_for_the_signed_in_subject() -> None:
    guard = csrf.Csrf(SECRET)
    token = guard.mint("ann")
    assert guard.genuine(token, "ann") and not guard.genuine(token, "bob")
    for forged in ("a" * 32 + "." + "b" * 32, "a" * 32, "x", ""):
        assert not guard.genuine(forged, "ann")
    with pytest.raises(ValueError, match="at least"):
        csrf.Csrf("short")


class FakeRequest:
    def __init__(self, cookie: str, sent: str, **headers: str) -> None:
        self.COOKIES = {csrf.COOKIE: cookie}
        self.POST = {csrf.FIELD: sent}
        self.headers = {k.replace("_", "-"): v for k, v in headers.items()}
        self.host = "app.test"
        self.scheme = "http"


def test_csrf_needs_cookie_field_signature_and_same_site() -> None:
    guard = csrf.Csrf(SECRET)
    good = guard.mint("ann")
    assert guard.valid(FakeRequest(good, good), "ann")  # type: ignore[arg-type]
    chosen = "a" * 32 + "." + "b" * 32  # an attacker's cookie and field, both equal
    for request in (
        FakeRequest(chosen, chosen),
        FakeRequest(good, guard.mint("ann")),
        FakeRequest(good, good, sec_fetch_site="cross-site"),
        FakeRequest(good, good, sec_fetch_site="same-site"),
        FakeRequest(good, good, origin="https://evil.test"),
        FakeRequest(good, good, origin="null"),
    ):
        assert not guard.valid(request, "ann")  # type: ignore[arg-type]
    assert guard.valid(FakeRequest(good, good, origin="http://app.test"), "ann")  # type: ignore[arg-type]
    assert guard.valid(FakeRequest(good, good, sec_fetch_site="same-origin"), "ann")  # type: ignore[arg-type]
    assert not guard.valid(FakeRequest(good, good), "bob")  # type: ignore[arg-type]


def test_forms_follow_the_model_and_convert_strictly() -> None:
    fields = {f.name: f for f in fields_for(Note, creating=True)}
    assert set(fields) == {"title", "body", "done", "rank", "due", "owner_id"}
    assert (fields["title"].kind, fields["body"].kind) == ("text", "textarea")
    assert fields["owner_id"].references is Owner and label_of("owner_id") == "Owner"
    assert convert(fields["rank"], "") == (True, None)
    assert convert(fields["rank"], " 7 ") == (True, 7)
    assert convert(fields["done"], None) == (True, False)
    assert convert(fields["body"], "a\r\nb") == (True, "a\nb")
    for field_name, text, message in (
        ("title", "", "required"),
        ("title", "x" * 21, "at most 20"),
        ("title", "a\x00b", "NUL"),
        ("rank", "1.5", "whole number"),
        ("rank", "99999999999999999999", "out of range"),
        ("due", "soon", "date and time"),
    ):
        with pytest.raises(FieldProblem, match=message):
            convert(fields[field_name], text)
    assert convert(fields["due"], "2026-11-13T09:30")[1].tzinfo is datetime.UTC


def test_unchanged_fields_are_never_rewritten_on_edit() -> None:
    fields = fields_for(Note, creating=False)
    due = datetime.datetime(2026, 11, 13, 9, 30, 15, 123456, tzinfo=datetime.UTC)
    original = {
        "title": "t",
        "body": "line\nbreak",
        "done": True,
        "rank": 3,
        "due": due,
        "owner_id": None,
    }
    submitted = {
        "title": "t",
        "body": "line\r\nbreak",
        "done": "on",
        "rank": "3",
        "due": "2026-11-13T09:30:15",
        "owner_id": "",
    }
    assert collect(fields, submitted, original) == ({}, {})  # nothing the user changed
    changed = {**submitted, "title": "new", "rank": ""}
    values, errors = collect(fields, changed, original)
    assert values == {"title": "new", "rank": None} and errors == {}


def test_small_ui_helpers() -> None:
    assert sparkline(()) == "" and sparkline((1,)) == ""
    assert sparkline((1, 3, 2)).startswith("<svg") and "polyline" in sparkline(
        (1, 3, 2)
    )
    assert page_window(5, 9) == [1, None, 3, 4, 5, 6, 7, None, 9]
    assert page_window(1, 2) == [1, 2]
    assert initials("Ada Lovelace") == "AL" and initials("  ") == "?"
    assert icon_name("house") == "house" and icon_name("<script>") == "file-text"


def test_registration_rules() -> None:
    admin = Admin(access=staff, secret=SECRET)
    admin.register(Note)
    for bad in (
        dict(model=Note),
        dict(model=Secret, slug="static"),
        dict(model=Secret, slug="search"),
        dict(model=Secret, slug="Bad Slug"),
    ):
        with pytest.raises(RegistrationError, match="slug"):
            admin.register(**bad)
    with pytest.raises(ValueError, match="prefix"):
        Admin(access=staff, secret=SECRET, prefix="/admin/")
    with pytest.raises(ValueError, match="at least"):
        Admin(access=staff, secret="short")
    with pytest.raises(ValueError, match="unknown theme"):
        Admin(access=staff, secret=SECRET, theme="nope")

    class Wrong(ModelAdmin[Note]):
        columns = ("title", "nonsense")

    with pytest.raises(RegistrationError, match="unknown columns"):
        admin.register(Secret, Wrong)  # type: ignore[misc]
    other = Admin(access=staff, secret=SECRET, dashboard=("missing",))
    with pytest.raises(RegistrationError, match="dashboard"):
        other.api()


def test_admin_pages_are_not_part_of_the_documented_api() -> None:
    app = build()
    document = app.openapi()
    validate(document)
    assert not any(path.startswith("/admin") for path in document["paths"])
    assert {r.path for r in app.routes} >= {
        "/admin/{model}/{key}/delete",
        "/admin/search",
    }


# --- with a database -----------------------------------------------------


TABLES = ("admin_notes", "admin_secrets", "admin_owners")


@pytest.fixture
async def site(pool: Pool) -> AsyncIterator[tuple[Client, Database]]:
    assert URL is not None
    db = await Database.connect(URL, max_size=1)
    for table in TABLES:
        await db.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    await db.execute(
        "CREATE TABLE admin_owners (id bigint PRIMARY KEY, name varchar(20) NOT NULL)"
    )
    await db.execute(
        "CREATE TABLE admin_notes (id uuid PRIMARY KEY, title varchar(20) NOT NULL UNIQUE, "
        "body text NOT NULL, done boolean NOT NULL, rank bigint, due timestamptz, "
        "owner_id bigint REFERENCES admin_owners(id) ON DELETE SET NULL)"
    )
    await db.execute(
        "CREATE TABLE admin_secrets (id bigint PRIMARY KEY, label varchar(20) NOT NULL, "
        "hidden boolean NOT NULL)"
    )
    SecretAdmin.saved = []
    yield await pool.start(build()), db
    for table in TABLES:
        await db.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    db.close()


def token_of(html: str) -> str:
    found = re.search(r'name="csrf" value="([0-9a-f]{32}\.[0-9a-f]{32})"', html)
    assert found, html[:500]
    return found.group(1)


async def session(client: Client, path: str) -> tuple[str, dict[str, str]]:
    """Open a page with a form; return its token and headers carrying the cookie."""
    page = await client.request("GET", path, headers=STAFF)
    assert page.status == 200, page.body
    cookie = page.headers["set-cookie"].split(";")[0]
    return token_of(page.body), {**STAFF, "cookie": cookie}


async def submit(
    client: Client, path: str, fields: dict[str, str], headers: dict[str, str]
) -> Any:
    return await client.request(
        "POST", path, urlencode(fields).encode(), content_type=FORM, headers=headers
    )


async def note_id(db: Database, title: str = "n") -> UUID:
    key = uuid4()
    await db.execute(
        "INSERT INTO admin_notes VALUES ($1, $2, '', false, NULL, NULL, NULL)",
        key,
        title,
    )
    return key


@needs_database
async def test_pages_need_the_policy_except_the_public_assets(
    site: tuple[Client, Database],
) -> None:
    client, _ = site
    for path in ("/admin", "/admin/notes", "/admin/notes/add", "/admin/search"):
        assert (await client.request("GET", path)).status == 401
    css = await client.request("GET", "/admin/static/admin.css")
    assert css.status == 200 and "--hn-accent" in css.body
    assert css.headers["cache-control"].endswith("immutable")
    script = await client.request("GET", "/admin/static/admin.js")
    assert script.status == 200 and "data-hn-theme-toggle" in script.body


@needs_database
async def test_dashboard_shows_stats_nav_and_safe_headers(
    site: tuple[Client, Database],
) -> None:
    client, db = site
    await note_id(db)
    page = await client.request("GET", "/admin", headers=STAFF)
    assert page.status == 200 and "hn-drawer" not in page.body
    assert "Test Admin" in page.body and "u-editor" in page.body
    assert 'class="hn-count">1<' in page.body and "Overview" in page.body
    assert 'data-theme="studio"' in page.body and "#i-bookmark" in page.body
    policy = page.headers["content-security-policy"]
    assert "form-action 'self'" in policy and "base-uri 'none'" in policy
    assert page.headers["cache-control"] == "no-store"
    assert "script-src" not in policy and "<script>" not in page.body


@needs_database
async def test_list_search_tabs_sorting_paging_and_escaping(
    site: tuple[Client, Database],
) -> None:
    client, db = site
    for title in ("alpha", "beta", "<script>x", "100%_done"):
        await note_id(db, title)
    await db.execute("UPDATE admin_notes SET done = true WHERE title = 'beta'")

    async def get(**query: str) -> Any:
        return await client.request(
            "GET", "/admin/notes", query=urlencode(query), headers=STAFF
        )

    first = await get()
    assert "1–2 of 4" in first.body and 'aria-current="page"' in first.body
    assert "<script>x" not in first.body
    assert "page=2" in (await get(page="2")).body
    assert (await get(page="99999999999999999999")).status == 200  # clamped, not a 500
    assert (await get(page="-3")).status == 200
    assert (
        "alpha" in (await get(q="ALPH")).body
        and "beta" not in (await get(q="ALPH")).body
    )
    assert (
        "100%_done" in (await get(q="%_")).body
        and "alpha" not in (await get(q="%_")).body
    )
    assert '"><b>' not in (await get(q='"><b>')).body
    done = await get(view="1")
    assert (
        "beta" in done.body
        and "alpha" not in done.body
        and "Open" not in done.body.split("<tbody>")[1]
    )
    # only "alpha" and "beta" contain an "a": the order must not depend on how the
    # database collates punctuation and digits (it differs between images)
    ordered = await get(sort="title", dir="desc", q="a")
    assert ordered.body.index("beta") < ordered.body.index("alpha")
    assert 'aria-sort="descending"' in ordered.body
    assert "No rows." in (await get(q="zzz")).body
    assert "hn-badge" in first.body


@needs_database
async def test_create_edit_and_delete_round_trip_with_csrf(
    site: tuple[Client, Database],
) -> None:
    client, db = site
    token, headers = await session(client, "/admin/notes/add")
    assert (
        await submit(
            client, "/admin/notes/add", {"title": "x", "csrf": "0" * 65}, headers
        )
    ).status == 403
    assert (
        await submit(client, "/admin/notes/add", {"title": "x", "csrf": token}, STAFF)
    ).status == 403
    assert await db.fetch("SELECT id FROM admin_notes") == []
    created = await submit(
        client,
        "/admin/notes/add",
        {"title": "First", "body": "hi", "rank": "3", "csrf": token},
        headers,
    )
    assert (created.status, created.headers["location"]) == (
        303,
        "/admin/notes?done=created",
    )
    row = (await db.fetch("SELECT * FROM admin_notes"))[0]
    assert (row["title"], row["done"], row["rank"]) == ("First", False, 3)
    assert (
        "Created."
        in (
            await client.request(
                "GET", "/admin/notes", query="done=created", headers=STAFF
            )
        ).body
    )

    path = f"/admin/notes/{row['id']}"
    token2, headers2 = await session(client, path)
    saved = await submit(
        client, path, {"title": "Second", "done": "on", "csrf": token2}, headers2
    )
    assert saved.status == 303
    updated = (await db.fetch("SELECT * FROM admin_notes"))[0]
    assert (updated["title"], updated["done"], updated["body"]) == ("Second", True, "")

    confirm = await client.request("GET", path + "/delete", headers=STAFF)
    assert "cannot be undone" in confirm.body
    token3, headers3 = await session(client, path + "/delete")
    assert (
        await submit(client, path + "/delete", {"csrf": "1" * 65}, headers3)
    ).status == 403
    assert len(await db.fetch("SELECT id FROM admin_notes")) == 1
    assert (
        await submit(client, path + "/delete", {"csrf": token3}, headers3)
    ).status == 303
    assert await db.fetch("SELECT id FROM admin_notes") == []


@needs_database
async def test_a_token_the_attacker_chose_or_a_cross_site_post_is_refused(
    site: tuple[Client, Database],
) -> None:
    client, db = site
    chosen = "a" * 32 + "." + "b" * 32
    forged = await submit(
        client,
        "/admin/notes/add",
        {"title": "evil", "csrf": chosen},
        {**STAFF, "cookie": f"hinoki_csrf={chosen}"},
    )
    assert forged.status == 403
    token, headers = await session(client, "/admin/notes/add")
    for extra in ({"sec-fetch-site": "cross-site"}, {"origin": "https://evil.test"}):
        reply = await submit(
            client,
            "/admin/notes/add",
            {"title": "evil", "csrf": token},
            {**headers, **extra},
        )
        assert reply.status == 403
    assert await db.fetch("SELECT id FROM admin_notes") == []


@needs_database
async def test_untouched_datetimes_survive_an_edit(
    site: tuple[Client, Database],
) -> None:
    client, db = site
    key = await note_id(db, "dated")
    await db.execute(
        "UPDATE admin_notes SET due = '2026-11-13 09:30:15.123456+00' WHERE id = $1",
        key,
    )
    path = f"/admin/notes/{key}"
    token, headers = await session(client, path)
    page = await client.request("GET", path, headers=STAFF)
    assert 'value="2026-11-13T09:30:15"' in page.body and 'step="1"' in page.body
    # what a browser submits: the shown value (it cannot show microseconds)
    reply = await submit(
        client,
        path,
        {"title": "dated", "due": "2026-11-13T09:30:15", "csrf": token},
        headers,
    )
    assert reply.status == 303
    stored = (await db.fetch("SELECT due FROM admin_notes WHERE id = $1", key))[0][
        "due"
    ]
    assert stored.microsecond == 123456  # untouched: never rewritten


@needs_database
async def test_invalid_input_gives_form_errors_not_server_errors(
    site: tuple[Client, Database],
) -> None:
    client, db = site
    await note_id(db, "taken")
    token, headers = await session(client, "/admin/notes/add")
    bad = await submit(
        client,
        "/admin/notes/add",
        {
            "title": "x" * 25,
            "rank": "99999999999999999999",
            "body": "<i>k</i>",
            "csrf": token,
        },
        headers,
    )
    assert bad.status == 422
    assert 'aria-invalid="true"' in bad.body and 'role="alert"' in bad.body
    assert "at most 20 characters" in bad.body and "out of range" in bad.body
    assert "&lt;i&gt;k&lt;/i&gt;" in bad.body and "<i>k</i>" not in bad.body
    nul = await submit(
        client, "/admin/notes/add", {"title": "a\x00b", "csrf": token}, headers
    )
    assert nul.status == 422 and "NUL" in nul.body
    duplicate = await submit(
        client, "/admin/notes/add", {"title": "taken", "csrf": token}, headers
    )
    assert duplicate.status == 422 and "already in use" in duplicate.body
    assert len(await db.fetch("SELECT id FROM admin_notes")) == 1
    # the failed insert did not poison the request's transaction
    ok = await submit(
        client, "/admin/notes/add", {"title": "fine", "csrf": token}, headers
    )
    assert ok.status == 303


@needs_database
async def test_foreign_keys_become_selects_and_fieldsets_group_fields(
    site: tuple[Client, Database],
) -> None:
    client, db = site
    await db.execute("INSERT INTO admin_owners VALUES (1, 'Ada'), (2, 'Bob')")
    page = await client.request("GET", "/admin/notes/add", headers=STAFF)
    assert "<select" in page.body and ">Owner<" in page.body
    assert (
        page.body.count("<option") == 3
        and "hn-fieldset" in page.body
        and "Organization" in page.body
    )
    token = token_of(page.body)
    cookie = {**STAFF, "cookie": page.headers["set-cookie"].split(";")[0]}
    await submit(
        client,
        "/admin/notes/add",
        {"title": "owned", "owner_id": "2", "csrf": token},
        cookie,
    )
    assert (await db.fetch("SELECT owner_id FROM admin_notes"))[0]["owner_id"] == 2


@needs_database
async def test_edit_opens_beside_the_list_in_drawer_themes_and_on_a_page_otherwise(
    pool: Pool, site: tuple[Client, Database]
) -> None:
    _, db = site
    key = await note_id(db, "shown")
    drawer = await (await pool.start(build("studio"))).request(
        "GET", f"/admin/notes/{key}", headers=STAFF
    )
    assert (
        "hn-drawer" in drawer.body
        and "hn-table" in drawer.body
        and 'class="is-open"' in drawer.body
    )
    page = await (await pool.start(build("paper"))).request(
        "GET", f"/admin/notes/{key}", headers=STAFF
    )
    assert (
        "hn-drawer" not in page.body
        and "hn-page-form" in page.body
        and 'data-theme="paper"' in page.body
    )


@needs_database
async def test_hidden_missing_and_forbidden_rows_look_the_same(
    site: tuple[Client, Database],
) -> None:
    client, db = site
    await db.execute(
        "INSERT INTO admin_secrets VALUES (1, 'visible', false), (2, 'hidden', true)"
    )
    listing = (await client.request("GET", "/admin/secrets", headers=STAFF)).body
    assert "visible" in listing and "/admin/secrets/2" not in listing
    for path in (
        "/admin/secrets/2",
        "/admin/secrets/99",
        "/admin/secrets/abc",
        "/admin/nothing",
        "/admin/notes/not-a-uuid",
        "/admin/secrets/1/delete",
    ):
        assert (await client.request("GET", path, headers=STAFF)).status == 404
    token, headers = await session(client, "/admin/secrets/1")
    assert (
        await submit(client, "/admin/secrets/1/delete", {"csrf": token}, headers)
    ).status == 404
    assert len(await db.fetch("SELECT id FROM admin_secrets")) == 2


@needs_database
async def test_a_write_cannot_move_a_row_out_of_the_callers_scope(
    site: tuple[Client, Database],
) -> None:
    client, db = site
    await db.execute("INSERT INTO admin_secrets VALUES (1, 'mine', false)")
    token, headers = await session(client, "/admin/secrets/1")
    moved = await submit(
        client,
        "/admin/secrets/1",
        {"label": "mine", "hidden": "on", "csrf": token},
        headers,
    )
    assert moved.status == 422 and "out of the rows you may manage" in moved.body
    assert (await db.fetch("SELECT hidden FROM admin_secrets"))[0]["hidden"] is False
    token2, headers2 = await session(client, "/admin/secrets/add")
    created = await submit(
        client,
        "/admin/secrets/add",
        {"id": "9", "label": "x", "hidden": "on", "csrf": token2},
        headers2,
    )
    assert created.status == 422
    assert len(await db.fetch("SELECT id FROM admin_secrets")) == 1


@needs_database
async def test_saving_goes_through_the_hook_and_untouched_rows_are_not_rewritten(
    site: tuple[Client, Database],
) -> None:
    client, db = site
    token, headers = await session(client, "/admin/secrets/add")
    await submit(
        client,
        "/admin/secrets/add",
        {"id": "5", "label": "via hook", "csrf": token},
        headers,
    )
    assert SecretAdmin.saved == [{"id": 5, "label": "via hook", "hidden": False}]
    token2, headers2 = await session(client, "/admin/secrets/5")
    await submit(
        client, "/admin/secrets/5", {"label": "via hook", "csrf": token2}, headers2
    )
    assert SecretAdmin.saved[-1] == {}  # nothing changed, nothing sent to the database


@needs_database
async def test_bulk_delete_asks_first_and_deletes_only_what_is_listed(
    site: tuple[Client, Database],
) -> None:
    client, db = site
    keys = [await note_id(db, f"n{i}") for i in range(3)]
    token, headers = await session(client, "/admin/notes")
    ask = await client.request(
        "POST",
        "/admin/notes/bulk",
        urlencode(
            [
                ("csrf", token),
                ("ids", str(keys[0])),
                ("ids", str(keys[1])),
                ("action", "delete"),
            ]
        ).encode(),
        content_type=FORM,
        headers=headers,
    )
    assert ask.status == 200 and "Delete 2 selected" in ask.body
    assert len(await db.fetch("SELECT id FROM admin_notes")) == 3
    forged = await client.request(
        "POST",
        "/admin/notes/bulk",
        urlencode(
            [("csrf", "0" * 65), ("ids", str(keys[0])), ("confirmed", "1")]
        ).encode(),
        content_type=FORM,
        headers=headers,
    )
    assert forged.status == 403
    done = await client.request(
        "POST",
        "/admin/notes/bulk",
        urlencode(
            [
                ("csrf", token),
                ("ids", str(keys[0])),
                ("ids", str(keys[1])),
                ("confirmed", "1"),
            ]
        ).encode(),
        content_type=FORM,
        headers=headers,
    )
    assert done.status == 303
    left = await db.fetch("SELECT id FROM admin_notes")
    assert [r["id"] for r in left] == [keys[2]]


@needs_database
async def test_global_search_groups_matches_by_model(
    site: tuple[Client, Database],
) -> None:
    client, db = site
    await note_id(db, "findme")
    await db.execute("INSERT INTO admin_owners VALUES (1, 'findme too')")
    reply = await client.request(
        "GET", "/admin/search", query="q=FINDME", headers=STAFF
    )
    assert reply.status == 200 and "findme" in reply.body and "Notes" in reply.body
    assert (
        "No results."
        in (
            await client.request("GET", "/admin/search", query="q=zzz", headers=STAFF)
        ).body
    )
    assert (
        "Type something"
        in (await client.request("GET", "/admin/search", headers=STAFF)).body
    )


def test_the_current_database_helper_is_importable() -> None:
    assert callable(current_database) and callable(use_database)
