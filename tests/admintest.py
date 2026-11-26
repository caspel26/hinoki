"""The admin extra: templates, themes, forms, CSRF and the pages (PostgreSQL)."""

import asyncio
import datetime
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast
from urllib.parse import urlencode
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from openapi_spec_validator import validate

from hinoki import Access, Caller, Hinoki, Mount, Response
from hinoki.admin import (
    THEMES,
    Admin,
    Avatar,
    Badge,
    BulkAction,
    Fieldset,
    ModelAdmin,
    Stat,
    Tab,
    Theme,
    Tone,
    action,
    csrf,
)
from hinoki.admin import views as admin_views
from hinoki.admin.components import (
    Components,
    icon_name,
    initials,
    page_window,
    plain_text,
    sparkline,
    tone_name,
)
from hinoki.admin.formpages import check_references, option_lists, render_fields
from hinoki.admin.forms import (
    FieldProblem,
    FormField,
    collect,
    control,
    convert,
    fields_for,
    input_text,
    label_of,
    render_field,
    to_datetime,
)
from hinoki.admin.lists import apply_state, matching, pagination_for, table_head
from hinoki.admin.modeladmin import OutOfScope
from hinoki.admin.parts import Event
from hinoki.admin.site import ListState, Site, guarded, number, parse_key
from hinoki.admin.templates import UI, Raw, TemplateError, Templates, escape
from hinoki.admin.views import (
    authorize_bulk_rows,
    authorized_bulk_action,
    bulk_keys,
    bulk_rows,
    index,
)
from hinoki.db import Database, DatabaseError, DatabaseModule
from hinoki.errors import ErrorDetail, ModelValidationError, RegistrationError
from hinoki.orm import (
    Model,
    OnDelete,
    current_database,
    field,
    foreign_key,
    timestamps,
    use_database,
)
from hinoki.settings import AppSettings
from hinoki.testing import ASGIClient
from hinoki.utils import utc_now
from tests.harness import Client, Pool
from tests.providers import TokenAuth

TEST_HOST = "app.test"
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
    owner_id: int | None = foreign_key(Owner, on_delete=OnDelete.SET_NULL, default=None)


async def mark_notes_done(rows: tuple[Note, ...], _principal: Caller[Any]) -> None:
    await Note.filter(id__in=[row.id for row in rows]).aupdate({"done": True})


async def fail_after_marking_notes(
    rows: tuple[Note, ...], principal: Caller[Any]
) -> None:
    await mark_notes_done(rows, principal)
    raise RuntimeError("simulated action failure")


class CallableActionHandler:
    async def __call__(self, _rows: tuple[Note, ...], _principal: Caller[Any]) -> None:
        return None


class RaceActionState:
    started: ClassVar[asyncio.Event | None] = None
    release: ClassVar[asyncio.Event | None] = None


async def wait_and_mark_notes(rows: tuple[Note, ...], _principal: Caller[Any]) -> None:
    assert RaceActionState.started is not None
    assert RaceActionState.release is not None
    RaceActionState.started.set()
    await RaceActionState.release.wait()
    await Note.filter(id__in=[row.id for row in rows]).aupdate({"done": True})


def sync_action_handler(_rows: tuple[Note, ...], _principal: Caller[Any]) -> None:
    return None


class NoteAdmin(ModelAdmin[Note]):
    label = "Notes"
    columns = ("title", "done", "rank")
    search = ("title", "body")
    per_page = 2
    bulk_delete = True
    bulk_actions: ClassVar[tuple[BulkAction[Any], ...]] = (
        BulkAction(
            name="mark-done",
            label="Mark selected done",
            confirm="Mark these notes as done?",
            handler=mark_notes_done,
        ),
        BulkAction(
            name="fail-after-marking",
            label="Fail after marking",
            confirm="Run the failing test action?",
            handler=fail_after_marking_notes,
        ),
    )
    show_count = True
    badges: ClassVar[dict[str, Any]] = {
        "done": {True: Badge("Done", "ok"), False: Badge("Open", "warn", "pencil")}
    }
    tabs = (Tab("Done", {"done": True}), Tab("Open", {"done": False}))
    fieldsets = (Fieldset("Organization", ("owner_id",)),)
    modified_field = "due"

    def can(self, principal: Caller[Any], action: str, row: Note | None = None) -> bool:
        return not (
            action == "mark-done" and row is not None and row.title == "blocked"
        )


class RaceNoteAdmin(NoteAdmin):
    bulk_actions: ClassVar[tuple[BulkAction[Any], ...]] = (
        *NoteAdmin.bulk_actions,
        BulkAction(
            name="race-update",
            label="Update selected",
            confirm="Update the selected notes?",
            handler=wait_and_mark_notes,
        ),
    )

    def can(self, principal: Caller[Any], action: str, row: Note | None = None) -> bool:
        return super().can(principal, action, row) and not (
            action == "race-update" and row is not None and row.title == "blocked"
        )


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


def build(
    theme: str = "studio",
    templates: str | None = None,
    note_admin: type[ModelAdmin[Note]] = NoteAdmin,
    mount: str = "",
) -> Hinoki:
    admin = Admin(
        access=staff, secret=SECRET, theme=theme, templates=templates, site="Test Admin"
    )
    admin.register(Note, note_admin, slug="notes")
    admin.register(Secret, SecretAdmin, slug="secrets")
    admin.register(Owner, slug="owners")
    app = Hinoki(
        openapi_url="/openapi.json", settings=AppSettings(allowed_hosts=(TEST_HOST,))
    )
    app.include(Mount(mount, admin.api()) if mount else admin.api())
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


def sidebar_with(logo: str) -> str:
    admin = Admin(access=staff, secret=SECRET, site="Brand", logo=logo)
    return admin.templates.render(
        "sidebar",
        home="/admin",
        site=admin.site,
        tagline="",
        logo=admin.logo_mark,
        nav=Raw(""),
        extra=Raw(""),
        user=Raw(""),
    )


def test_the_logo_is_the_bookmark_unless_an_icon_or_a_path_is_given() -> None:
    assert "#i-bookmark" in sidebar_with("")
    assert "#i-layers" in sidebar_with("layers")
    image = sidebar_with("/static/logo.svg?v=1&x=<2>")
    assert (
        '<img class="hn-logo-img" src="/static/logo.svg?v=1&amp;x=&lt;2&gt;"' in image
    )
    assert "#i-bookmark" not in image


@pytest.mark.parametrize(
    "logo",
    [
        "not-an-icon",
        "https://cdn.example.com/logo.svg",
        "//cdn.example.com/logo.svg",
        '/logo.svg" onerror="x',
        "javascript:alert(1)",
    ],
)
def test_a_logo_the_admin_cannot_show_is_refused_when_it_is_built(logo: str) -> None:
    with pytest.raises(ValueError, match="logo"):
        Admin(access=staff, secret=SECRET, logo=logo)


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


def test_four_themes_share_the_component_set_and_validate() -> None:
    assert set(THEMES) == {"studio", "console", "paper", "bento"}
    assert {t.edit for t in THEMES.values()} == {"drawer", "page"}
    for name in THEMES:
        css = Templates(theme=name).stylesheet().decode()
        assert f':root[data-theme="{name}"]' in css and "--hn-accent:" in css
    for theme in THEMES.values():  # every style ships both schemes, system-driven
        css = Templates(theme=theme.name).stylesheet().decode()
        assert "prefers-color-scheme: dark" in css
        assert "[data-mode=dark]" in css and "[data-mode=light]" in css
    assert {t.layout for t in THEMES.values()} == {"sidebar", "topbar", "index", "rail"}
    assert {t.list_style for t in THEMES.values()} == {"table", "rows", "cards"}
    assert THEMES["studio"].layout == "sidebar"
    assert THEMES["studio"].list_style == "table"
    assert THEMES["bento"].layout == "rail"
    assert THEMES["bento"].list_style == "cards"
    assert THEMES["bento"].edit == "page"
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


def test_theme_rejects_incomplete_fixed_tokens_and_invalid_layout_choices() -> None:
    paper = THEMES["paper"]
    cases = (
        (Theme("bad", "Bad", "page", paper.light, paper.dark, {}), "fixed tokens"),
        (
            Theme("bad", "Bad", "modal", paper.light, paper.dark, paper.fixed),
            "bad edit or default_mode",
        ),
        (
            Theme(
                "bad",
                "Bad",
                "page",
                paper.light,
                paper.dark,
                paper.fixed,
                layout="unknown",
            ),
            "bad layout or list_style",
        ),
        (
            Theme(
                "bad name",
                "Bad",
                "page",
                paper.light,
                paper.dark,
                paper.fixed,
            ),
            "theme names are letters",
        ),
    )
    for theme, message in cases:
        with pytest.raises(ValueError, match=message):
            theme.check()


def test_component_fragments_support_hidden_avatar_names_and_typed_cells() -> None:
    components = Components(Templates())
    hidden_name = str(components.avatar("Ada Lovelace", show_name=False))
    assert "Ada Lovelace" not in hidden_name
    assert "AL" in hidden_name
    assert "AL" in str(components.cell(Avatar("Ada Lovelace")))
    raw = Raw("<strong>trusted</strong>")
    assert components.cell(raw) is raw


def test_csrf_tokens_are_signed_for_the_signed_in_subject() -> None:
    guard = csrf.Csrf(SECRET)
    token = guard.mint("ann")
    assert guard.genuine(token, "ann") and not guard.genuine(token, "bob")
    for forged in ("a" * 32 + "." + "b" * 32, "a" * 32, "x", ""):
        assert not guard.genuine(forged, "ann")
    with pytest.raises(ValueError, match="at least"):
        csrf.Csrf("short")


class FakeRequest:
    def __init__(
        self, cookie: str, sent: str, *, host: str = "app.test", **headers: str
    ) -> None:
        self.COOKIES = {csrf.COOKIE: cookie}
        self.POST = {csrf.FIELD: sent}
        self.headers = {k.replace("_", "-"): v for k, v in headers.items()}
        self.host = host
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
    assert guard.valid(
        FakeRequest(  # type: ignore[arg-type]
            good,
            good,
            origin="http://APP.TEST:80",
            host="app.test",
        ),
        "ann",
    )
    assert guard.valid(FakeRequest(good, good, sec_fetch_site="same-origin"), "ann")  # type: ignore[arg-type]
    assert not guard.valid(FakeRequest(good, good), "bob")  # type: ignore[arg-type]


def test_bulk_action_declarations_are_validated_on_registration() -> None:
    async def noop(_rows: tuple[Note, ...], _principal: Caller[Any]) -> None:
        return None

    class DuplicateActions(ModelAdmin[Note]):
        bulk_actions = (
            BulkAction("publish", "Publish", "Publish?", noop),
            BulkAction("publish", "Publish again", "Publish again?", noop),
        )

    class ReservedAction(ModelAdmin[Note]):
        bulk_actions = (BulkAction("delete", "Delete", "Delete?", noop),)

    class SyncAction(ModelAdmin[Note]):
        bulk_actions = (
            BulkAction(
                "publish",
                "Publish",
                "Publish?",
                sync_action_handler,  # type: ignore[arg-type]
            ),
        )

    class MissingHandler(ModelAdmin[Note]):
        bulk_actions = (
            BulkAction("missing", "Missing", "Run?", None),  # type: ignore[arg-type]
        )

    class NonCallableHandler(ModelAdmin[Note]):
        bulk_actions = (
            BulkAction("invalid", "Invalid", "Run?", 42),  # type: ignore[arg-type]
        )

    class CallableActions(ModelAdmin[Note]):
        bulk_actions = (
            BulkAction(
                "callable",
                "Callable action",
                "Run callable?",
                CallableActionHandler(),
            ),
        )

    for admin_class, reason in (
        (DuplicateActions, "duplicate bulk action"),
        (ReservedAction, "reserved bulk action name"),
        (SyncAction, "handler must be an async callable"),
        (MissingHandler, "handler must be an async callable"),
        (NonCallableHandler, "handler must be an async callable"),
    ):
        admin = Admin(access=staff, secret=SECRET)
        with pytest.raises(RegistrationError, match=reason):
            admin.register(Note, admin_class, slug="notes")

    Admin(access=staff, secret=SECRET).register(Note, CallableActions, slug="notes")


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


def test_form_helpers_cover_uuid_defaults_and_readonly_fields() -> None:
    identifier = FormField(
        "identifier", "Identifier", "uuid", True, False, False, None, UUID
    )
    with pytest.raises(FieldProblem, match="valid UUID"):
        convert(identifier, "not-a-uuid")

    defaulted = FormField("title", "Title", "text", True, False, True, None, str)
    assert convert(defaulted, "") == (False, None)

    shown_only = FormField(
        "computed",
        "Computed",
        "readonly",
        False,
        False,
        False,
        None,
        object,
        shown_only=True,
    )
    assert collect([shown_only], {"computed": "spoofed"}) == ({}, {})
    assert "disabled" in str(control(shown_only, "stored", None))
    rendered = str(render_field(Templates(), shown_only, "stored", None))
    assert "Not editable here." in rendered


def test_form_controls_cover_empty_select_checkbox_and_error_states() -> None:
    optional_text = FormField(
        "nickname", "Nickname", "text", False, False, False, 12, str
    )
    assert convert(optional_text, "") == (True, "")
    text_html = str(
        render_field(Templates(), optional_text, "", "<invalid>", options=None)
    )
    assert 'maxlength="12"' in text_html and 'aria-invalid="true"' in text_html
    assert "&lt;invalid&gt;" in text_html
    required = FormField("title", "Title", "text", True, False, False, None, str)
    assert " required" in str(control(required, "", None))

    choice = FormField(
        "choice", "Choice", "text", False, True, False, None, str, select=True
    )
    empty_select = str(control(choice, None, None, options=[("a", "Alpha")]))
    unknown_select = str(control(choice, "missing", None, options=[("a", "Alpha")]))
    assert 'value="">—</option>' in empty_select
    assert 'value="missing" selected>missing</option>' in unknown_select

    checkbox = FormField(
        "active", "Active", "checkbox", False, False, False, None, bool
    )
    assert "checked" in str(control(checkbox, True, None))
    assert "checked" not in str(control(checkbox, False, None))

    textarea = FormField("body", "Body", "textarea", False, False, False, None, str)
    assert "<textarea" in str(control(textarea, "\nfirst", None))


def test_admin_list_helpers_cover_single_search_ordering_and_pagination_gaps() -> None:
    single = cast(ModelAdmin[Any], SimpleNamespace(search=("title",)))
    searched = matching(single, Note.all(), "needle")
    assert searched.filters == (("title", "contains", ["needle"]),)

    ordered_entry = cast(
        ModelAdmin[Any],
        SimpleNamespace(
            tabs=(),
            search=(),
            model=Note,
            shown_columns=lambda: ("title",),
            ordering=("-title",),
        ),
    )
    ordered = apply_state(
        ordered_entry,
        Note.all(),
        ListState(sort="not-visible", direction="asc"),
    )
    assert ordered.order == (("title", True),)

    extra_column = cast(
        ModelAdmin[Any],
        SimpleNamespace(model=Note, shown_columns=lambda: ("virtual",)),
    )
    headings = str(
        table_head(cast(Any, SimpleNamespace()), "notes", extra_column, bulk=False)
    )
    assert "Virtual" in headings and '<th scope="col">' in headings

    site = cast(
        Any,
        SimpleNamespace(
            state=ListState(),
            parts=SimpleNamespace(pagination=lambda urls, summary: (urls, summary)),
            url=lambda *parts, **_query: "/" + "/".join(parts),
        ),
    )
    links, _summary = cast(
        tuple[list[tuple[str, str, bool]], str],
        pagination_for(site, "notes", 10, 1, 20),
    )
    assert any(label == "…" for _, label, _current in links)


async def test_admin_form_helpers_cover_large_and_unregistered_relations() -> None:
    owner_field = FormField(
        "owner_id", "Owner", "uuid", True, False, False, None, UUID, references=Owner
    )

    class OversizedQuery:
        async def limit(self, count: int) -> list[object]:
            assert count == 201
            return [object()] * count

    related = SimpleNamespace(
        model=Owner,
        can=lambda _principal, _action: True,
        queryset=lambda _principal: OversizedQuery(),
    )
    admin = SimpleNamespace(
        models={"owners": related}, templates=Templates(), timezone=datetime.UTC
    )
    site = cast(Site, SimpleNamespace(admin=admin, principal=object()))
    fields, options = await option_lists(site, [owner_field])
    assert fields == [owner_field] and options == {}

    missing_admin = SimpleNamespace(
        models={}, templates=Templates(), timezone=datetime.UTC
    )
    absent_site = cast(Site, SimpleNamespace(admin=missing_admin, principal=object()))
    assert (
        await check_references(absent_site, [owner_field], {"owner_id": uuid4()}) == {}
    )


def test_admin_fieldsets_without_renderable_fields_are_skipped() -> None:
    admin = SimpleNamespace(templates=Templates(), timezone=datetime.UTC)
    site = cast(Site, SimpleNamespace(admin=admin))
    entry = cast(
        ModelAdmin[Any],
        SimpleNamespace(fieldset_list=(Fieldset("Empty", ("not_a_field",)),)),
    )
    assert str(render_fields(site, entry, [], {}, {}, {})) == ""


def test_modeladmin_display_and_config_fallbacks() -> None:
    admin = NoteAdmin()
    admin.model = Note
    row = Note(id=uuid4(), title="", done=False, rank=None)
    assert admin.title_of(row) == f"Note {row.id}"
    assert admin.skipped() == frozenset()


def test_admin_site_key_and_number_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    assert number("invalid", 4, 10) == 4
    entry = cast(ModelAdmin[Any], SimpleNamespace(model=Note))
    assert parse_key(entry, "\x00") is None
    monkeypatch.setattr("hinoki.admin.site.key_type", lambda _model: str)
    assert parse_key(entry, "slug") == "slug"


def test_admin_user_card_includes_an_escaped_logout_link() -> None:
    admin = SimpleNamespace(
        users=None,
        logout_url='/logout?next="<home>',
        templates=Templates(),
        role=lambda _principal: "Staff",
    )
    fake_site = cast(
        Site,
        SimpleNamespace(admin=admin, principal=Caller(id="Ada", user="Ada")),
    )
    rendered = str(Site.user_card(fake_site))
    assert 'href="/logout?next=&quot;&lt;home&gt;"' in rendered


async def test_admin_guarded_writes_summarize_validation_and_database_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def atomic() -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(
        "hinoki.admin.site.current_database",
        lambda: SimpleNamespace(atomic=atomic),
    )

    async def invalid() -> None:
        raise ModelValidationError([ErrorDetail("title", "invalid", "is invalid")])

    async def rejected() -> None:
        raise DatabaseError("database rejected values")

    assert await guarded(invalid()) == "title: is invalid"
    assert await guarded(rejected()) == "The database rejected these values."


async def test_admin_bulk_resolution_rejects_bad_or_inaccessible_selections() -> None:
    principal = Caller(id="admin", user="admin")
    admin = NoteAdmin()
    admin.model = Note
    key = uuid4()
    row = SimpleNamespace(id=key)
    assert bulk_keys(admin, ["not-a-uuid"], None) == []
    assert bulk_keys(admin, ["not-a-uuid"], "publish") is None
    assert bulk_keys(admin, [str(key), str(key)], None) is None
    assert bulk_keys(admin, [], "publish") == []
    assert authorize_bulk_rows(admin, principal, [key, uuid4()], [row], None) == [row]
    assert authorized_bulk_action(admin, principal, "not-a-real-action") is None
    fake_site = cast(Site, SimpleNamespace(principal=principal))
    assert await bulk_rows(fake_site, admin, ["invalid"], "publish") is None
    assert await bulk_rows(fake_site, admin, [], "publish") == []


@pytest.mark.parametrize(
    ("dialect", "lock"),
    [
        ("postgres", ' FOR UPDATE OF "' + Note.__table__ + '"'),
        ("mysql", " FOR UPDATE"),
        ("mariadb", " FOR UPDATE"),
        ("sqlite", ""),
    ],
)
async def test_admin_bulk_lock_clause_matches_the_dialect(
    monkeypatch: pytest.MonkeyPatch, dialect: str, lock: str
) -> None:
    seen: list[str] = []

    async def fetch(sql: str, *params: Any) -> list[Any]:
        seen.append(sql)
        return []

    monkeypatch.setattr(
        "hinoki.admin.views.current_database",
        lambda: SimpleNamespace(dialect=dialect, fetch=fetch),
    )
    principal = Caller(id="admin", user="admin")
    admin = NoteAdmin()
    admin.model = Note
    fake_site = cast(Site, SimpleNamespace(principal=principal))
    await bulk_rows(fake_site, admin, [str(uuid4())], "publish")
    assert seen
    assert seen[0].endswith(lock) if lock else "FOR UPDATE" not in seen[0]
    assert ("$1" in seen[0]) == (dialect == "postgres")


async def test_admin_dashboard_skips_hidden_entry_and_renders_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def activity(_principal: Caller[Any]) -> list[Event]:
        return [Event("Changed a note", "now", icon="<bad>", tone="unknown")]

    admin = Admin(
        access=staff,
        secret=SECRET,
        dashboard=("hidden",),
        activity=activity,
    )

    class FakeSite:
        def __init__(self, *_args: object) -> None:
            self.admin = admin
            self.content = ""

        def entry(self, _slug: str) -> None:
            return None

        def header(self, *_args: object) -> str:
            return "header"

        def done_notice(self) -> str:
            return ""

        async def page(self, _title: str, content: Raw, **_kwargs: object) -> Response:
            self.content = str(content)
            return Response(200, self.content)

    monkeypatch.setattr(admin_views, "Site", FakeSite)
    caller = Caller(id="staff", user="staff")
    fake_self = SimpleNamespace(admin=admin)
    response = await index(cast(Any, fake_self), cast(Any, None), caller)
    assert response.status == 200
    assert "Recent activity" in response.body.decode()
    assert "Changed a note" in response.body.decode()


def test_admin_bulk_row_authorization_fails_closed_for_custom_actions() -> None:
    principal = Caller(id="admin", user="admin")
    key = uuid4()
    row = SimpleNamespace(id=key)
    denied_view = cast(
        ModelAdmin[Any],
        SimpleNamespace(model=Note, can=lambda _p, _a, _r=None: False),
    )
    assert authorize_bulk_rows(denied_view, principal, [key], [row], None) == []
    assert authorize_bulk_rows(denied_view, principal, [key], [row], "publish") is None
    assert (
        authorize_bulk_rows(denied_view, principal, [uuid4()], [row], "publish") is None
    )


@needs_database
async def test_admin_write_views_handle_missing_models_rows_and_csrf(
    site: tuple[Client, Database],
) -> None:
    client, database = site
    assert (
        await client.request("GET", "/admin/missing/add", headers=STAFF)
    ).status == 404
    assert (
        await submit(client, "/admin/missing/add", {"csrf": "x"}, STAFF)
    ).status == 404
    missing = uuid4()
    assert (
        await submit(client, f"/admin/notes/{missing}", {"csrf": "x"}, STAFF)
    ).status == 404
    existing = await note_id(database, "csrf")
    assert (await submit(client, f"/admin/notes/{existing}", {}, STAFF)).status == 403


@needs_database
async def test_admin_bulk_views_report_bad_targets_empty_sets_and_failures(
    site: tuple[Client, Database], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, database = site
    assert (await submit(client, "/admin/missing/bulk", {}, STAFF)).status == 404
    assert (
        await submit(
            client, "/admin/notes/bulk", {"action": "missing", "csrf": "x"}, STAFF
        )
    ).status == 404
    token, headers = await session(client, "/admin/notes")
    empty = await submit(client, "/admin/notes/bulk", {"csrf": token}, headers)
    assert empty.status == 303

    key = await note_id(database, "bulk-failure")

    async def failed_action(*_args: Any) -> str:
        return "action refused"

    monkeypatch.setattr("hinoki.admin.views.run_bulk_action", failed_action)
    failed = await submit(
        client,
        "/admin/notes/bulk",
        {
            "csrf": token,
            "action": "mark-done",
            "confirmed": "1",
            "ids": str(key),
        },
        headers,
    )
    assert failed.status == 409 and "action refused" in failed.body


@needs_database
async def test_admin_delete_guard_failure_is_reported_as_conflict(
    site: tuple[Client, Database], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, database = site
    key = await note_id(database, "delete-conflict")

    async def refuse_remove(
        _self: NoteAdmin, _row: Note, _principal: Caller[Any]
    ) -> None:
        raise OutOfScope

    monkeypatch.setattr("tests.admintest.NoteAdmin.remove", refuse_remove)
    token, headers = await session(client, f"/admin/notes/{key}")
    response = await submit(
        client, f"/admin/notes/{key}/delete", {"csrf": token}, headers
    )
    assert response.status == 409
    assert "move the row out" in response.body


def test_modeladmin_rejects_invalid_bulk_action_shapes_and_text() -> None:
    class ListActions(ModelAdmin[Note]):
        bulk_actions: ClassVar[Any] = []

    class WrongItem(ModelAdmin[Note]):
        bulk_actions = (object(),)  # type: ignore[assignment]

    class BlankLabel(ModelAdmin[Note]):
        bulk_actions = (BulkAction("publish", " ", "Confirm?", mark_notes_done),)

    class BlankConfirmation(ModelAdmin[Note]):
        bulk_actions = (BulkAction("publish", "Publish", " ", mark_notes_done),)

    for admin_class, reason in (
        (ListActions, "bulk_actions must be a tuple"),
        (WrongItem, "bulk_actions must contain BulkAction values"),
        (BlankLabel, "label must contain"),
        (BlankConfirmation, "confirmation must contain"),
    ):
        admin = Admin(access=staff, secret=SECRET)
        with pytest.raises(RegistrationError, match=reason):
            admin.register(Note, admin_class, slug="notes")


def test_modeladmin_rejects_unknown_fieldset_columns() -> None:
    class WrongFieldset(ModelAdmin[Note]):
        fieldsets = (Fieldset("Details", ("not_a_column",)),)

    with pytest.raises(RegistrationError, match="fieldset 'Details' names"):
        Admin(access=staff, secret=SECRET).register(Note, WrongFieldset, slug="notes")


@needs_database
async def test_modeladmin_missing_scoped_rows_raise_not_found(
    site: tuple[Client, Database],
) -> None:
    _, database = site
    admin = NoteAdmin()
    admin.model = Note
    row = Note(id=uuid4(), title="missing")
    principal = Caller(id="admin", user="admin")
    with use_database(database):
        with pytest.raises(Note.NotFound, match="Note not found"):
            await admin.save({"title": "updated"}, principal, row)
        with pytest.raises(Note.NotFound, match="Note not found"):
            await admin.remove(row, principal)


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


def test_admin_datetime_uses_app_timezone_and_rejects_dst_edges() -> None:
    rome = ZoneInfo("Europe/Rome")
    instant = datetime.datetime(2026, 7, 1, 8, 30, tzinfo=datetime.UTC)
    assert input_text(instant, rome) == "2026-07-01T10:30:00"
    assert plain_text(instant, rome) == "2026-07-01 10:30"
    due_field = next(
        field for field in fields_for(Note, creating=False) if field.name == "due"
    )
    rendered = str(render_field(Templates(), due_field, instant, None, zone=rome))
    assert 'value="2026-07-01T10:30:00"' in rendered
    assert to_datetime("2026-07-01T10:30", rome) == instant
    assert to_datetime("2026-07-01T10:30+02:00", rome) == instant
    with pytest.raises(FieldProblem, match="does not exist"):
        to_datetime("2026-03-29T02:30", rome)
    with pytest.raises(FieldProblem, match="ambiguous"):
        to_datetime("2026-10-25T02:30", rome)

    values, errors = collect([due_field], {"due": "2026-03-29T02:30"}, zone=rome)
    assert values == {} and "due" in errors


def test_admin_inherits_timezone_from_including_app() -> None:
    admin = Admin(access=staff, secret=SECRET)
    app = Hinoki(settings=AppSettings(timezone="Europe/Rome"))
    app.include(admin.api())
    assert admin.timezone == ZoneInfo("Europe/Rome")


def test_admin_rejects_conflicting_app_timezones() -> None:
    admin = Admin(access=staff, secret=SECRET)
    admin.bind_timezone(ZoneInfo("Europe/Rome"))
    with pytest.raises(RegistrationError, match="different timezones"):
        admin.bind_timezone(ZoneInfo("America/New_York"))


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
    for model, slug in (
        (Note, None),
        (Secret, "static"),
        (Secret, "search"),
        (Secret, "Bad Slug"),
    ):
        with pytest.raises(RegistrationError, match="slug"):
            admin.register(model, slug=slug)
    with pytest.raises(ValueError, match="prefix"):
        Admin(access=staff, secret=SECRET, prefix="/admin/")
    with pytest.raises(ValueError, match="at least"):
        Admin(access=staff, secret="short")
    with pytest.raises(ValueError, match="unknown theme"):
        Admin(access=staff, secret=SECRET, theme="nope")

    class Wrong(ModelAdmin):  # type: ignore[type-arg]
        columns = ("title", "nonsense")

    with pytest.raises(RegistrationError, match="unknown columns"):
        admin.register(Secret, Wrong)
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
    yield await pool.start(build(), host=TEST_HOST), db
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


async def postgres_update_is_waiting(db: Database, pid: int) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 3
    while loop.time() < deadline:
        activity = await db.fetch_one(
            "SELECT wait_event_type, query FROM pg_stat_activity WHERE pid = $1", pid
        )
        if (
            activity is not None
            and activity["wait_event_type"] == "Lock"
            and "UPDATE admin_notes" in activity["query"]
        ):
            return True
        await asyncio.sleep(0.01)
    return False


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
    drawer = await (await pool.start(build("studio"), host=TEST_HOST)).request(
        "GET", f"/admin/notes/{key}", headers=STAFF
    )
    assert (
        "hn-drawer" in drawer.body
        and "hn-table" in drawer.body
        and 'class="is-open"' in drawer.body
    )
    page = await (await pool.start(build("paper"), host=TEST_HOST)).request(
        "GET", f"/admin/notes/{key}", headers=STAFF
    )
    assert (
        "hn-drawer" not in page.body
        and "hn-page-form" in page.body
        and 'data-theme="paper"' in page.body
    )
    bento = await (await pool.start(build("bento"), host=TEST_HOST)).request(
        "GET", f"/admin/notes/{key}", headers=STAFF
    )
    assert (
        "hn-drawer" not in bento.body
        and "hn-page-form" in bento.body
        and 'data-theme="bento"' in bento.body
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
async def test_custom_bulk_action_confirms_then_shows_a_success_notice(
    site: tuple[Client, Database],
) -> None:
    client, db = site
    selected = [await note_id(db, "first"), await note_id(db, "second")]
    await note_id(db, "untouched")
    token, headers = await session(client, "/admin/notes")

    ask = await submit(
        client,
        "/admin/notes/bulk",
        {
            "csrf": token,
            "action": "mark-done",
            "ids": str(selected[0]),
        },
        headers,
    )
    assert ask.status == 200
    assert "Mark these notes as done?" in ask.body
    assert "(1 selected)" in ask.body
    assert ">Mark selected done</button>" in ask.body
    assert 'name="action" value="mark-done"' in ask.body
    assert not any(
        row["done"] for row in await db.fetch("SELECT done FROM admin_notes")
    )

    done = await submit(
        client,
        "/admin/notes/bulk",
        {
            "csrf": token,
            "action": "mark-done",
            "confirmed": "1",
            "ids": str(selected[0]),
        },
        headers,
    )
    assert done.status == 303
    assert "done=action%3Anotes%3Amark-done" in done.headers["location"]
    notice = await client.request(
        "GET",
        "/admin/notes",
        query="done=action%3Anotes%3Amark-done",
        headers=STAFF,
    )
    assert notice.status == 200 and "Mark selected done completed." in notice.body
    states = {
        str(row["id"]): row["done"]
        for row in await db.fetch("SELECT id, done FROM admin_notes")
    }
    assert states[str(selected[0])] is True
    assert states[str(selected[1])] is False


@needs_database
async def test_custom_bulk_action_holds_row_lock_through_the_handler(
    pool: Pool, site: tuple[Client, Database]
) -> None:
    """A concurrent permission-field change waits until the action commits."""
    _, db = site
    key = await note_id(db, "race")
    client = await pool.start(build(note_admin=RaceNoteAdmin), host=TEST_HOST)
    token, headers = await session(client, "/admin/notes")
    RaceActionState.started = asyncio.Event()
    RaceActionState.release = asyncio.Event()
    writer = await Database.connect(URL or "", max_size=1)
    action_task = asyncio.create_task(
        submit(
            client,
            "/admin/notes/bulk",
            {
                "csrf": token,
                "action": "race-update",
                "confirmed": "1",
                "ids": str(key),
            },
            headers,
        )
    )
    update_done = asyncio.Event()
    writer_ready = asyncio.Event()
    writer_pid: int | None = None

    async def change_permission_field() -> None:
        nonlocal writer_pid
        async with writer.atomic():
            row = await writer.fetch_one("SELECT pg_backend_pid() AS pid")
            assert row is not None
            writer_pid = int(row["pid"])
            writer_ready.set()
            await writer.execute(
                "UPDATE admin_notes SET title = 'blocked' WHERE id = $1", key
            )
        update_done.set()

    update_task: asyncio.Task[None] | None = None
    try:
        assert RaceActionState.started is not None
        await asyncio.wait_for(RaceActionState.started.wait(), timeout=3)
        update_task = asyncio.create_task(change_permission_field())
        await asyncio.wait_for(writer_ready.wait(), timeout=3)
        assert writer_pid is not None
        assert await postgres_update_is_waiting(db, writer_pid), (
            "the concurrent permission update did not wait on a row lock"
        )
        RaceActionState.release.set()
        response = await asyncio.wait_for(action_task, timeout=3)
        assert response.status == 303
        await asyncio.wait_for(update_task, timeout=3)
    finally:
        if RaceActionState.release is not None:
            RaceActionState.release.set()
        if not action_task.done():
            await action_task
        if update_task is not None and not update_task.done():
            await update_task
        RaceActionState.started = None
        RaceActionState.release = None
        writer.close()

    row = (await db.fetch("SELECT title, done FROM admin_notes WHERE id = $1", key))[0]
    assert row == {"title": "blocked", "done": True}


@needs_database
async def test_custom_bulk_action_rechecks_all_row_permissions_and_ids(
    site: tuple[Client, Database],
) -> None:
    client, db = site
    allowed = await note_id(db, "allowed")
    denied = await note_id(db, "blocked")
    token, headers = await session(client, "/admin/notes")

    forbidden = await client.request(
        "POST",
        "/admin/notes/bulk",
        urlencode(
            [
                ("csrf", token),
                ("action", "mark-done"),
                ("ids", str(allowed)),
                ("ids", str(denied)),
            ]
        ).encode(),
        content_type=FORM,
        headers=headers,
    )
    assert forbidden.status == 404
    assert not any(
        row["done"] for row in await db.fetch("SELECT done FROM admin_notes")
    )

    duplicate = await client.request(
        "POST",
        "/admin/notes/bulk",
        urlencode(
            [
                ("csrf", token),
                ("action", "mark-done"),
                ("ids", str(allowed)),
                ("ids", str(allowed)),
            ]
        ).encode(),
        content_type=FORM,
        headers=headers,
    )
    assert duplicate.status == 400

    stale = await submit(
        client,
        "/admin/notes/bulk",
        {"csrf": token, "action": "mark-done", "ids": str(uuid4())},
        headers,
    )
    assert stale.status == 404


@needs_database
async def test_custom_bulk_action_rechecks_permissions_on_confirmation_and_rolls_back(
    site: tuple[Client, Database],
) -> None:
    client, db = site
    changed = await note_id(db, "allowed")
    token, headers = await session(client, "/admin/notes")
    forged = await submit(
        client,
        "/admin/notes/bulk",
        {"csrf": "0" * 65, "action": "mark-done", "ids": str(changed)},
        headers,
    )
    assert forged.status == 403
    ask = await submit(
        client,
        "/admin/notes/bulk",
        {"csrf": token, "action": "mark-done", "ids": str(changed)},
        headers,
    )
    assert ask.status == 200
    await db.execute("UPDATE admin_notes SET title = 'blocked' WHERE id = $1", changed)
    refused = await submit(
        client,
        "/admin/notes/bulk",
        {
            "csrf": token,
            "action": "mark-done",
            "confirmed": "1",
            "ids": str(changed),
        },
        headers,
    )
    assert refused.status == 404
    assert (await db.fetch("SELECT done FROM admin_notes WHERE id = $1", changed))[0][
        "done"
    ] is False

    failing = await note_id(db, "fails")
    failed = await submit(
        client,
        "/admin/notes/bulk",
        {"csrf": token, "action": "fail-after-marking", "ids": str(failing)},
        headers,
    )
    assert failed.status == 200
    response = await submit(
        client,
        "/admin/notes/bulk",
        {
            "csrf": token,
            "action": "fail-after-marking",
            "confirmed": "1",
            "ids": str(failing),
        },
        headers,
    )
    assert response.status == 500
    assert (await db.fetch("SELECT done FROM admin_notes WHERE id = $1", failing))[0][
        "done"
    ] is False


@needs_database
async def test_search_looks_in_every_configured_column(
    site: tuple[Client, Database],
) -> None:
    client, db = site
    for title, body, done in (
        ("first", "needle in the body", True),
        ("second", "needle again", False),
        ("needle title", "", False),
        ("other", "50%_off", False),
    ):
        await db.execute(
            "INSERT INTO admin_notes VALUES ($1, $2, $3, $4, NULL, NULL, NULL)",
            uuid4(),
            title,
            body,
            done,
        )

    async def listed(**query: str) -> str:
        reply = await client.request(
            "GET", "/admin/notes", query=urlencode(query), headers=STAFF
        )
        assert reply.status == 200
        return str(reply.body)

    everywhere = await listed(q="NEEDLE")
    assert "of 3" in everywhere and "other" not in everywhere
    # The tab narrows the match; it is not an alternative to it.
    open_only = await listed(q="needle", view="2")
    assert "of 2" in open_only and "first" not in open_only
    literal = await listed(q="%_")
    assert "other" in literal and "of 1" in literal
    found = await client.request("GET", "/admin/search", query="q=again", headers=STAFF)
    assert "second" in found.body and "first" not in found.body


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


def test_shared_stylesheet_keeps_form_text_and_boundaries_legible() -> None:
    css = (UI / "base.css").read_text(encoding="utf-8")
    assert "::placeholder { color: var(--hn-muted); opacity: 1; }" in css
    assert "border: 1px solid var(--hn-field)" in css
    assert "--hn-field: color-mix(" in css
    assert ".hn-page.hn-gap" in css


def test_pagination_gap_marker_is_not_dimmed_as_a_control() -> None:
    html = str(Components(Templates()).pagination([("", "…", False)], "1"))
    assert 'class="hn-page hn-gap"' in html


class HidingNotes(NoteAdmin):
    def can(self, principal: Any, action: str, row: Note | None = None) -> bool:
        if action == "view" and row is not None and row.title.startswith("hide"):
            return False
        return super().can(principal, action, row)


@needs_database
async def test_rows_the_caller_may_not_view_are_not_listed(
    site: tuple[Client, Database], pool: Pool
) -> None:
    _, db = site
    await note_id(db, "show-me")
    await note_id(db, "hide-me")
    client = await pool.start(build(note_admin=HidingNotes), host=TEST_HOST)
    for path in ("/admin/notes", "/admin"):
        page = await client.request("GET", path, headers=STAFF)
        assert page.status == 200
        assert "hide-me" not in page.body
    assert (
        "show-me" in (await client.request("GET", "/admin/notes", headers=STAFF)).body
    )


@needs_database
async def test_the_admin_under_a_mount_builds_every_url_from_the_prefix(
    site: tuple[Client, Database], pool: Pool
) -> None:
    client = await pool.start(build(mount="/ops"), host=TEST_HOST)
    page = await client.request("GET", "/ops/admin", headers=STAFF)
    assert page.status == 200
    assert 'href="/ops/admin/notes' in page.body
    assert 'href="/admin' not in page.body and 'src="/admin' not in page.body
    assert "Path=/ops/admin" in page.headers["set-cookie"]
    asset = re.search(r'href="(/ops/admin/static/admin\.css[^"]*)"', page.body)
    assert asset is not None
    assert (await client.request("GET", asset.group(1).split("?")[0])).status == 200
    token, headers = await session(client, "/ops/admin/notes/add")
    saved = await submit(
        client, "/ops/admin/notes/add", {"csrf": token, "title": "mounted"}, headers
    )
    assert saved.status in {200, 302, 303}


# --- declaration forms ---------------------------------------------------


class DeclaredNoteAdmin(ModelAdmin, model=Note):  # type: ignore[type-arg]
    columns = ("title", "done")
    badges = {  # noqa: RUF012
        "done": {True: ("Done", Tone.OK), False: ("Open", Tone.WARN, "pencil")}
    }
    tabs = ("done",)
    fieldsets = {"Status": ("done",)}  # noqa: RUF012

    @action(confirm="Mark these notes as done?")
    async def mark_all_done(
        self, rows: tuple[Note, ...], principal: Caller[Any]
    ) -> None:
        await Note.filter(id__in=[row.id for row in rows]).aupdate({"done": True})

    @action("Reopen", confirm="Reopen the selected notes?")
    async def reopen(self, rows: tuple[Note, ...], principal: Caller[Any]) -> None:
        await Note.filter(id__in=[row.id for row in rows]).aupdate({"done": False})


class OwnSlugAdmin(ModelAdmin, model=Secret):  # type: ignore[type-arg]
    slug = "vault"


def test_the_model_is_a_class_keyword_and_matches_the_generic() -> None:
    assert DeclaredNoteAdmin.model is Note  # type: ignore[misc]

    class Generic(ModelAdmin[Note]):
        pass

    class Both(ModelAdmin[Note], model=Note):
        pass

    assert Generic.model is Both.model is Note  # type: ignore[misc]
    with pytest.raises(RegistrationError, match="contradicts"):

        class Clash(ModelAdmin[Note], model=Secret):
            pass


def admin_of(*items: Any, **options: Any) -> Admin:
    """A site with each model or ModelAdmin class registered, one call each."""
    site = Admin(access=staff, secret=SECRET, **options)
    for item in items:
        site.register(item)
    return site


def test_register_takes_models_and_admin_classes_and_decorates() -> None:
    admin = admin_of(Owner, DeclaredNoteAdmin, OwnSlugAdmin)
    assert list(admin.models) == ["admin-owners", "admin-notes", "vault"]
    assert isinstance(admin.models["admin-notes"], DeclaredNoteAdmin)
    assert admin.models["vault"].model is Secret
    with pytest.raises(RegistrationError, match="names no model"):
        admin_of(ModelAdmin)
    site = Admin(access=staff, secret=SECRET)

    @site.register
    class Decorated(ModelAdmin, model=Note):  # type: ignore[type-arg]
        slug = "decorated"

    assert site.models["decorated"].model is Note
    assert issubclass(Decorated, ModelAdmin)  # the class comes back unchanged
    with pytest.raises(RegistrationError, match="is for Note, not Secret"):
        Admin(access=staff, secret=SECRET).register(Secret, DeclaredNoteAdmin)


def test_tones_are_an_enum_and_strings_still_work() -> None:
    assert str(Tone.OK) == "ok" and str(Tone.WARN) == "warn"
    assert Badge("Published", Tone.OK).tone == Badge("Published", "ok").tone
    assert Stat("Posts", 3).tone is Tone.ACCENT
    assert tone_name(Tone.DANGER) == "danger" and tone_name("nonsense") == "neutral"


def test_badge_tabs_and_fieldset_short_forms_are_expanded() -> None:
    admin = admin_of(DeclaredNoteAdmin)
    entry = admin.models["admin-notes"]
    assert entry.badges["done"][True] == Badge("Done", Tone.OK)
    assert entry.badges["done"][False] == Badge("Open", Tone.WARN, "pencil")
    assert [(tab.label, dict(tab.filters)) for tab in entry.tab_list] == [
        ("Done", {"done": True}),
        ("Open", {"done": False}),
    ]
    assert [(item.title, item.fields) for item in entry.fieldset_list] == [
        ("Status", ("done",))
    ]


def test_tabs_by_column_need_badges_for_that_column() -> None:
    class NoBadges(ModelAdmin, model=Note):  # type: ignore[type-arg]
        tabs = ("done",)

    with pytest.raises(RegistrationError, match="has no entry in badges"):
        admin_of(NoBadges)


def test_action_methods_become_bulk_actions_with_derived_names() -> None:
    admin = admin_of(DeclaredNoteAdmin)
    actions = admin.models["admin-notes"].bulk_actions
    assert [(a.name, a.label, a.confirm) for a in actions] == [
        (
            "mark-all-done",
            "Mark all done selected",
            "Mark these notes as done?",
        ),
        ("reopen", "Reopen", "Reopen the selected notes?"),
    ]
    assert not DeclaredNoteAdmin.bulk_actions


def test_a_mounted_admin_lives_at_the_mount_unless_it_names_a_prefix() -> None:
    plain = admin_of(Owner)
    assert plain.prefix == "/admin"
    app = Hinoki()
    app.include(Mount("/ops", plain))
    assert plain.prefix == "" and plain.base == "/ops"
    assert {r.path for r in app.routes} >= {"/ops", "/ops/search"}

    named = admin_of(Owner, prefix="/staff")
    other = Hinoki()
    other.include(Mount("/ops", named))
    assert named.base == "/ops/staff"
    assert "/ops/staff/search" in {r.path for r in other.routes}

    unmounted = Hinoki()
    unmounted.include(admin_of(Owner))
    assert "/admin/search" in {r.path for r in unmounted.routes}


@needs_database
async def test_an_action_method_confirms_and_runs_like_a_bulk_action(
    pool: Pool, site: tuple[Client, Database]
) -> None:
    _, db = site
    chosen = await note_id(db, "declared")
    client = await pool.start(build(note_admin=DeclaredNoteAdmin), host=TEST_HOST)
    listing = await client.request("GET", "/admin/notes", headers=STAFF)
    assert listing.status == 200
    assert "Done" in listing.body and "Open" in listing.body  # tabs from the badges
    form = await client.request("GET", "/admin/notes/add", headers=STAFF)
    assert form.status == 200 and "Status" in form.body  # the fieldset title
    token, headers = await session(client, "/admin/notes")
    ask = await submit(
        client,
        "/admin/notes/bulk",
        {"csrf": token, "action": "mark-all-done", "ids": str(chosen)},
        headers,
    )
    assert ask.status == 200 and "Mark these notes as done?" in ask.body
    done = await submit(
        client,
        "/admin/notes/bulk",
        {
            "csrf": token,
            "action": "mark-all-done",
            "confirmed": "1",
            "ids": str(chosen),
        },
        headers,
    )
    assert done.status == 303
    rows = await db.fetch("SELECT done FROM admin_notes")
    assert [row["done"] for row in rows] == [True]


# --- automatic defaults --------------------------------------------------


@timestamps()
class Stamped(Model, table="admin_stamped"):
    id: int = field(primary_key=True)
    name: str = field(max_length=20)
    note: str = ""
    code: str = field(default="x", max_length=5)
    secret: str = ""
    count: int = 0
    updated_at: datetime.datetime = field(default_factory=utc_now)


class PlainStampedAdmin(ModelAdmin, model=Stamped):  # type: ignore[type-arg]
    exclude = ("secret",)


def test_a_model_admin_with_no_settings_gets_working_defaults() -> None:
    admin = admin_of(PlainStampedAdmin)
    entry = admin.models["admin-stamped"]
    assert entry.search == ("name", "note", "code")  # first three shown text columns
    assert "secret" not in entry.search and "count" not in entry.search
    assert entry.modified_field == "updated_at"
    assert (entry.per_page, entry.show_count, entry.bulk_delete) == (20, False, True)


def test_explicit_settings_win_over_the_defaults() -> None:
    class Own(ModelAdmin, model=Stamped):  # type: ignore[type-arg]
        search = ("note",)
        modified_field = "name"
        per_page = 5
        show_count = True
        bulk_delete = False

    site = Admin(access=staff, secret=SECRET, per_page=50, show_count=True)
    site.register(Stamped, Own)
    entry = site.models["admin-stamped"]
    assert (entry.search, entry.modified_field) == (("note",), "name")
    assert (entry.per_page, entry.show_count, entry.bulk_delete) == (5, True, False)
    inherited = Admin(access=staff, secret=SECRET, per_page=50, show_count=True)
    inherited.register(Stamped)
    assert (inherited.models["admin-stamped"].per_page, True) == (50, True)
    assert inherited.models["admin-stamped"].show_count is True


def test_search_defaults_never_reach_columns_the_list_does_not_show() -> None:
    class Narrow(ModelAdmin, model=Stamped):  # type: ignore[type-arg]
        columns = ("count", "name")

    entry = admin_of(Narrow).models["admin-stamped"]
    assert entry.search == ("name",)


def test_a_model_without_timestamps_has_no_modified_line() -> None:
    entry = admin_of(Owner).models["admin-owners"]
    assert entry.modified_field == ""


def test_site_admin_decorates_a_class_that_does_not_name_its_model() -> None:
    site = Admin(access=staff, secret=SECRET)

    @site.admin(Note)
    class Notes(ModelAdmin):  # type: ignore[type-arg]
        columns = ("title",)

    @site.admin(Owner, slug="people")
    class Owners(ModelAdmin):  # type: ignore[type-arg]
        pass

    assert issubclass(Notes, ModelAdmin)  # the class comes back unchanged
    assert site.models["admin-notes"].model is Note
    assert site.models["people"].model is Owner
    assert isinstance(site.models["admin-notes"], Notes)


def test_site_admin_refuses_a_class_for_another_model_or_a_non_admin() -> None:
    site = Admin(access=staff, secret=SECRET)

    class ForNote(ModelAdmin, model=Note):  # type: ignore[type-arg]
        pass

    with pytest.raises(RegistrationError, match="is for Note, not Secret"):
        site.admin(Secret)(ForNote)
    with pytest.raises(RegistrationError, match="decorates a ModelAdmin class"):
        site.admin(Note)(int)  # type: ignore[type-var]
    site.admin(Note)(ForNote)  # naming the same model twice is fine


async def test_an_admin_mounted_at_the_root_links_back_to_the_root(
    tmp_path: Path,
) -> None:
    target = f"sqlite://{tmp_path / 'root.sqlite3'}"
    setup = await Database.connect(target, max_size=1)
    await setup.execute("CREATE TABLE admin_owners (id integer PRIMARY KEY, name text)")
    setup.close()
    admin = Admin(access=staff, secret=SECRET, prefix="", site="Test Admin")
    admin.register(Owner, slug="owners")
    app = Hinoki(
        openapi_url="/openapi.json", settings=AppSettings(allowed_hosts=(TEST_HOST,))
    )
    app.include(admin.api())
    app.install(DatabaseModule(target, max_size=1))
    async with ASGIClient(app, host=TEST_HOST) as client:
        page = await client.request("GET", "/", headers=STAFF)
    body = page.body
    assert page.status == 200 and 'href=""' not in body
    assert 'href="/"' in body and 'href="/owners"' in body
