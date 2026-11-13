"""The admin extra: components, forms, CSRF and the pages (PostgreSQL)."""

import os
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from openapi_spec_validator import validate

from hinoki import Access, HinokiAPI, Principal, RegistrationError
from hinoki.admin import Admin, ModelAdmin, csrf
from hinoki.admin.forms import FieldProblem, collect, convert, fields_for
from hinoki.admin.templates import Raw, TemplateError, Templates, escape
from hinoki.db import Database, DatabaseModule
from hinoki.orm import Model, field
from tests.harness import Client, Pool
from tests.providers import TokenAuth

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
needs_database = pytest.mark.skipif(
    URL is None, reason="HINOKI_TEST_DATABASE_URL is not set"
)
STAFF = {"authorization": "Bearer editor-token"}
FORM = "application/x-www-form-urlencoded"
staff = Access(auth=TokenAuth(), scopes=["posts:write"])


class Note(Model, table="admin_notes"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str = field(max_length=20)
    body: str = ""
    done: bool = False
    rank: int | None = None


class NoteAdmin(ModelAdmin[Note]):
    columns = ("title", "done", "rank")
    search = ("title",)
    per_page = 2


class Secret(Model, table="admin_secrets"):
    id: int = field(primary_key=True)
    label: str = field(max_length=20)
    hidden: bool = False


class SecretAdmin(ModelAdmin[Secret]):
    saved: ClassVar[list[dict[str, Any]]] = []

    def queryset(self, principal: Principal[Any]) -> Any:
        return Secret.filter(hidden=False)

    def can(self, principal: Principal[Any], action: str, row: Any = None) -> bool:
        return action != "delete"

    async def save(
        self, values: dict[str, Any], principal: Principal[Any], row: Any = None
    ) -> Any:
        self.saved.append(values)
        return await super().save(values, principal, row)


def build(templates: str | None = None) -> HinokiAPI:
    admin = Admin(access=staff, templates=templates, site="Test Admin")
    admin.register(Note, NoteAdmin, slug="notes")
    admin.register(Secret, SecretAdmin, slug="secrets")
    app = HinokiAPI(openapi_url="/openapi.json")
    app.include(admin.api())
    if URL:
        app.install(DatabaseModule(URL, max_size=3))
    return app


# --- no database ---------------------------------------------------------


def test_templates_escape_by_default_and_guard_raw_values() -> None:
    templates = Templates()
    html = templates.render("notice", kind="error", role="alert", message="<b>&\"'")
    assert "&lt;b&gt;&amp;&quot;&#x27;" in html
    assert "<b>" not in html
    with pytest.raises(TemplateError, match="raw but not a Raw"):
        templates.render(
            "layout",
            title="t",
            site="s",
            stylesheet="c",
            home="/",
            user="u",
            nav="<ul></ul>",
            notice=Raw(""),
            content=Raw(""),
        )
    with pytest.raises(TemplateError, match="no value for"):
        templates.render("notice", kind="x")
    with pytest.raises(TemplateError, match="unknown component"):
        templates.render("../../etc/passwd")
    assert escape("<a>") == "&lt;a&gt;"


def test_application_templates_override_the_built_in_theme(tmp_path: Path) -> None:
    (tmp_path / "notice.html").write_text("<p class='mine'>{{ message }}</p>")
    (tmp_path / "admin.css").write_text("body{color:red}")
    templates = Templates(tmp_path)
    assert templates.render("notice", message="hi") == "<p class='mine'>hi</p>"
    assert templates.stylesheet() == b"body{color:red}"
    assert "<table" in Templates(tmp_path).sources["table"]  # others stay built in


def test_forms_follow_the_model_and_convert_strictly() -> None:
    fields = {f.name: f for f in fields_for(Note, frozenset(), creating=True)}
    assert set(fields) == {"title", "body", "done", "rank"}  # id has a default
    assert (fields["title"].kind, fields["body"].kind) == ("text", "textarea")
    assert (fields["done"].kind, fields["rank"].kind) == ("checkbox", "number")
    assert fields["title"].required and not fields["rank"].required
    first = next(iter(fields_for(Secret, frozenset(), creating=True)))
    assert first.name == "id"
    assert "id" not in {f.name for f in fields_for(Secret, frozenset(), creating=False)}
    assert convert(fields["rank"], "") == (True, None)
    assert convert(fields["rank"], " 7 ") == (True, 7)
    assert convert(fields["done"], None) == (True, False)
    assert convert(fields["done"], "on") == (True, True)
    assert convert(fields["body"], "") == (True, "")
    with pytest.raises(FieldProblem, match="required"):
        convert(fields["title"], "")
    with pytest.raises(FieldProblem, match="at most 20"):
        convert(fields["title"], "x" * 21)
    with pytest.raises(FieldProblem, match="whole number"):
        convert(fields["rank"], "1.5")
    values, errors = collect(list(fields.values()), {"title": "", "rank": "x"})
    assert set(errors) == {"title", "rank"} and values == {"body": "", "done": False}


def test_csrf_needs_cookie_and_field_to_agree() -> None:
    class Fake:
        def __init__(self, cookie: str | None, field: str | None) -> None:
            self.COOKIES = {csrf.COOKIE: cookie} if cookie else {}
            self.POST = {csrf.FIELD: field} if field else {}

    good = "a" * 32
    assert csrf.valid(Fake(good, good))  # type: ignore[arg-type]
    for cookie, sent in (
        (good, "b" * 32),
        (None, good),
        (good, None),
        ("x", "x"),
        (good.upper(), good.upper()),
    ):
        assert not csrf.valid(Fake(cookie, sent))  # type: ignore[arg-type]


def test_registration_rules_and_documentation() -> None:
    admin = Admin(access=staff)
    admin.register(Note)
    with pytest.raises(RegistrationError, match="slug"):
        admin.register(Note)
    with pytest.raises(RegistrationError, match="slug"):
        admin.register(Secret, slug="static")
    with pytest.raises(RegistrationError, match="slug"):
        admin.register(Secret, slug="Bad Slug")
    with pytest.raises(ValueError, match="prefix"):
        Admin(access=staff, prefix="/admin/")
    app = HinokiAPI()
    app.include(admin.api())
    document = app.openapi()
    validate(document)
    # The admin is a set of HTML pages, not part of the documented API.
    assert not any(path.startswith("/admin") for path in document["paths"])
    assert {r.path for r in app.routes} >= {"/admin/{model}/{key}/delete"}


# --- with a database -----------------------------------------------------


@pytest.fixture
async def site(pool: Pool, tmp_path: Path) -> AsyncIterator[tuple[Client, Database]]:
    assert URL is not None
    admin_db = await Database.connect(URL, max_size=1)
    for table in ("admin_notes", "admin_secrets"):
        await admin_db.execute(f"DROP TABLE IF EXISTS {table}")
    await admin_db.execute(
        "CREATE TABLE admin_notes (id uuid PRIMARY KEY, title varchar(20) NOT NULL, "
        "body text NOT NULL, done boolean NOT NULL, rank bigint)"
    )
    await admin_db.execute(
        "CREATE TABLE admin_secrets (id bigint PRIMARY KEY, "
        "label varchar(20) NOT NULL, "
        "hidden boolean NOT NULL)"
    )
    SecretAdmin.saved = []
    yield await pool.start(build()), admin_db
    for table in ("admin_notes", "admin_secrets"):
        await admin_db.execute(f"DROP TABLE IF EXISTS {table}")
    admin_db.close()


def token_of(html: str) -> str:
    found = re.search(r'name="csrf" value="([0-9a-f]{32})"', html)
    assert found, html
    return found.group(1)


async def form_session(client: Client, path: str) -> tuple[str, dict[str, str]]:
    """Open a form page; return its token and the headers that carry the cookie."""
    page = await client.request("GET", path, headers=STAFF)
    assert page.status == 200
    token = token_of(page.body)
    cookie = page.headers["set-cookie"].split(";")[0]
    return token, {**STAFF, "cookie": cookie}


async def submit(
    client: Client, path: str, fields: dict[str, str], headers: dict[str, str]
) -> Any:
    return await client.request(
        "POST", path, urlencode(fields).encode(), content_type=FORM, headers=headers
    )


@needs_database
async def test_every_page_needs_the_access_policy_except_the_stylesheet(
    site: tuple[Client, Database],
) -> None:
    client, _ = site
    for path in ("/admin", "/admin/notes", "/admin/notes/add"):
        assert (await client.request("GET", path)).status == 401
    css = await client.request("GET", "/admin/static/admin.css")
    assert css.status == 200 and "--accent" in css.body
    assert css.headers["content-type"].startswith("text/css")


@needs_database
async def test_index_lists_models_with_counts_and_safe_headers(
    site: tuple[Client, Database],
) -> None:
    client, db = site
    await db.execute(
        "INSERT INTO admin_notes VALUES ($1, 'a', '', false, NULL)", uuid4()
    )
    page = await client.request("GET", "/admin", headers=STAFF)
    assert page.status == 200
    assert "Note" in page.body and "1 row(s)" in page.body and "u-editor" in page.body
    assert "Skip to content" in page.body and "<h1>Dashboard</h1>" in page.body
    assert page.headers["content-security-policy"].startswith("default-src 'self'")
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]


@needs_database
async def test_list_searches_paginates_and_escapes(
    site: tuple[Client, Database],
) -> None:
    client, db = site
    for title in ("alpha", "beta", "<script>x</script>", "100%_done"):
        await db.execute(
            "INSERT INTO admin_notes VALUES ($1, $2, '', false, NULL)", uuid4(), title
        )
    first = await client.request("GET", "/admin/notes", headers=STAFF)
    assert "4 row(s)" in first.body and "page 1 of 2" in first.body
    assert "<script>x" not in first.body  # escaped wherever it appears
    second = await client.request("GET", "/admin/notes", query="page=2", headers=STAFF)
    assert "page 2 of 2" in second.body and "Previous" in second.body
    found = await client.request("GET", "/admin/notes", query="q=ALPH", headers=STAFF)
    assert "alpha" in found.body and "beta" not in found.body
    literal = await client.request("GET", "/admin/notes", query="q=%25_", headers=STAFF)
    assert "100%_done" in literal.body and "alpha" not in literal.body
    xss = await client.request(
        "GET", "/admin/notes", query="q=%22%3E%3Cb%3E", headers=STAFF
    )
    assert '"><b>' not in xss.body and "&quot;&gt;&lt;b&gt;" in xss.body
    empty = await client.request("GET", "/admin/notes", query="q=zzz", headers=STAFF)
    assert "No rows." in empty.body


@needs_database
async def test_create_edit_and_delete_round_trip_with_csrf(
    site: tuple[Client, Database],
) -> None:
    client, db = site
    token, headers = await form_session(client, "/admin/notes/add")
    refused = await submit(
        client, "/admin/notes/add", {"title": "x", "csrf": "0" * 32}, headers
    )
    assert refused.status == 403
    no_cookie = await submit(
        client, "/admin/notes/add", {"title": "x", "csrf": token}, STAFF
    )
    assert no_cookie.status == 403
    assert await db.fetch("SELECT id FROM admin_notes") == []

    created = await submit(
        client,
        "/admin/notes/add",
        {"title": "First", "body": "hello", "rank": "3", "csrf": token},
        headers,
    )
    assert created.status == 303
    assert created.headers["location"] == "/admin/notes?done=created"
    row = (await db.fetch("SELECT * FROM admin_notes"))[0]
    assert (row["title"], row["done"], row["rank"]) == ("First", False, 3)

    listing = await client.request(
        "GET", "/admin/notes", query="done=created", headers=STAFF
    )
    assert "Created." in listing.body and 'role="status"' in listing.body

    edit_path = f"/admin/notes/{row['id']}"
    token2, headers2 = await form_session(client, edit_path)
    assert (
        'value="First"' in (await client.request("GET", edit_path, headers=STAFF)).body
    )
    saved = await submit(
        client, edit_path, {"title": "Second", "done": "on", "csrf": token2}, headers2
    )
    assert saved.status == 303
    updated = (await db.fetch("SELECT * FROM admin_notes"))[0]
    assert (updated["title"], updated["done"], updated["body"]) == ("Second", True, "")

    confirm = await client.request("GET", edit_path + "/delete", headers=STAFF)
    assert "cannot be undone" in confirm.body
    token3, headers3 = await form_session(client, edit_path + "/delete")
    blocked = await submit(client, edit_path + "/delete", {"csrf": "1" * 32}, headers3)
    assert (
        blocked.status == 403 and len(await db.fetch("SELECT id FROM admin_notes")) == 1
    )
    gone = await submit(client, edit_path + "/delete", {"csrf": token3}, headers3)
    assert gone.status == 303
    assert await db.fetch("SELECT id FROM admin_notes") == []


@needs_database
async def test_invalid_forms_rerender_with_errors_and_keep_input(
    site: tuple[Client, Database],
) -> None:
    client, db = site
    token, headers = await form_session(client, "/admin/notes/add")
    reply = await submit(
        client,
        "/admin/notes/add",
        {"title": "x" * 25, "rank": "abc", "body": "<i>keep</i>", "csrf": token},
        headers,
    )
    assert reply.status == 422
    assert 'aria-invalid="true"' in reply.body and 'role="alert"' in reply.body
    assert (
        "at most 20 characters" in reply.body and "enter a whole number" in reply.body
    )
    assert "&lt;i&gt;keep&lt;/i&gt;" in reply.body and "<i>keep</i>" not in reply.body
    assert await db.fetch("SELECT id FROM admin_notes") == []


@needs_database
async def test_missing_hidden_and_forbidden_rows_look_the_same(
    site: tuple[Client, Database],
) -> None:
    client, db = site
    await db.execute("INSERT INTO admin_secrets VALUES (1, 'visible', false)")
    await db.execute("INSERT INTO admin_secrets VALUES (2, 'hidden', true)")
    assert (
        "visible" in (await client.request("GET", "/admin/secrets", headers=STAFF)).body
    )
    listing = (await client.request("GET", "/admin/secrets", headers=STAFF)).body
    assert "/admin/secrets/2" not in listing  # the hidden row is not listed
    for path in (
        "/admin/secrets/2",
        "/admin/secrets/99",
        "/admin/secrets/abc",
        "/admin/nothing",
        "/admin/notes/not-a-uuid",
    ):
        assert (await client.request("GET", path, headers=STAFF)).status == 404
    # can() forbids delete: the page does not exist for this model
    assert (
        await client.request("GET", "/admin/secrets/1/delete", headers=STAFF)
    ).status == 404
    token, headers = await form_session(client, "/admin/secrets/1")
    forbidden = await submit(
        client, "/admin/secrets/1/delete", {"csrf": token}, headers
    )
    assert forbidden.status == 404
    assert len(await db.fetch("SELECT id FROM admin_secrets")) == 2


@needs_database
async def test_saving_goes_through_the_model_admin_hook(
    site: tuple[Client, Database],
) -> None:
    client, db = site
    token, headers = await form_session(client, "/admin/secrets/add")
    created = await submit(
        client,
        "/admin/secrets/add",
        {"id": "5", "label": "via hook", "csrf": token},
        headers,
    )
    assert created.status == 303
    assert SecretAdmin.saved == [{"id": 5, "label": "via hook", "hidden": False}]
    # a row the queryset hides cannot be updated through its URL
    await db.execute("INSERT INTO admin_secrets VALUES (6, 'h', true)")
    token2, headers2 = await form_session(client, "/admin/secrets/5")
    miss = await submit(
        client, "/admin/secrets/6", {"label": "x", "csrf": token2}, headers2
    )
    assert miss.status == 404


@needs_database
async def test_a_custom_template_directory_is_used(
    pool: Pool, site: tuple[Client, Database], tmp_path: Path
) -> None:
    _, _ = site
    (tmp_path / "notice.html").write_text('<div class="custom">{{ message }}</div>')
    client = await pool.start(build(str(tmp_path)))
    page = await client.request("GET", "/admin/nothing", headers=STAFF)
    assert page.status == 404 and 'class="custom"' in page.body
