"""``hinoki llms``, the llms.txt renderer and the opt-in ``llms_url`` page."""

from pathlib import Path

import pytest

from hinoki import Access, Hinoki, Schema, http
from hinoki.__main__ import main
from hinoki.ai.llms import render_llms
from hinoki.settings import AppSettings
from tests.harness import Pool
from tests.providers import TokenAuth

TARGET = "tests.llmstest:make_app"


class NoteIn(Schema):
    title: str


class NoteOut(Schema):
    id: int
    title: str


class Search(Schema):
    text: str


@http.get("/notes", tags=["notes"], operation_id="listNotes")
def listing() -> list[NoteOut]:
    """List the notes."""
    return []


@http.post("/notes", tags=["notes"], access=Access(TokenAuth()), status=201)
def create(body: NoteIn) -> NoteOut:
    """Create a note."""
    return NoteOut(id=1, title=body.title)


@http.query("/notes/search", tags=["notes"])
def search(body: Search) -> list[NoteOut]:
    """Search the notes."""
    return []


@http.get("/old", deprecated=True)
def old() -> NoteOut:
    return NoteOut(id=1, title="old")


@http.get("/ops/reindex", access=Access(TokenAuth()), include_in_schema=False)
def reindex() -> NoteOut:
    return NoteOut(id=1, title="secret")


def make_app(**options: str | None) -> Hinoki:
    app = Hinoki(title="Notes", **options)  # type: ignore[arg-type]
    app.include(reindex, old, search, create, listing)
    return app


def test_the_text_lists_operations_by_tag_with_their_facts() -> None:
    text = render_llms(make_app().openapi(), openapi_url="/openapi.json")
    assert text.startswith("# Notes\n\n> Version 0.1.0\n")
    assert "framework docs for agents: https://hinoki.caspel.dev/llms.txt" in text
    assert "[/openapi.json](/openapi.json)" in text
    assert text.index("## notes") < text.index("## Operations")
    assert (
        "- `GET /notes` (listNotes): List the notes. — response: 200 list of NoteOut"
        in text
    )
    post = next(row for row in text.splitlines() if row.startswith("- `POST /notes`"))
    assert "security: TokenAuth" in post and "request: NoteIn" in post
    assert post.endswith("response: 201 NoteOut")
    assert "- `GET /old`" in text and "deprecated; response: 200 NoteOut" in text
    assert "/ops/reindex" not in text and "secret" not in text
    assert render_llms(make_app().openapi()) == render_llms(make_app().openapi())


@pytest.mark.parametrize("version", ["3.1", "3.2"])
def test_query_operations_are_listed_in_both_document_versions(version: str) -> None:
    document = make_app(openapi_version=version).openapi()
    text = render_llms(document)
    assert "- `QUERY /notes/search`" in text
    assert "request: Search" in text


def test_the_output_is_sorted_by_path_then_method() -> None:
    text = render_llms(make_app().openapi())
    rows = [row for row in text.splitlines() if row.startswith("- `")]
    notes = [row.split("`")[1] for row in rows if "/notes" in row]
    assert notes == ["GET /notes", "POST /notes", "QUERY /notes/search"]


def test_text_taken_from_the_document_cannot_start_a_block() -> None:
    document = {
        "info": {"title": "A\n# B", "version": "1"},
        "paths": {"/x": {"get": {"summary": "one\n\n## two"}}},
    }
    lines = render_llms(document).splitlines()
    assert [row for row in lines if row.startswith("#")] == ["# A # B", "## Operations"]


def test_the_command_prints_writes_and_checks_the_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["llms", TARGET]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("# Notes\n") and "/ops/reindex" not in printed
    output = tmp_path / "llms.txt"
    assert main(["llms", TARGET, "--output", str(output)]) == 0
    assert capsys.readouterr().out == ""
    assert output.read_text(encoding="utf-8") == printed
    assert main(["llms", TARGET, "--check", str(output)]) == 0
    output.write_text("stale\n", encoding="utf-8")
    assert main(["llms", TARGET, "--check", str(output)]) == 1
    error = capsys.readouterr().err
    assert f"hinoki llms {TARGET} --output {output}" in error


def test_the_command_reports_a_missing_file_and_a_bad_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["llms", TARGET, "--check", str(tmp_path / "none.txt")]) == 2
    assert "does not exist" in capsys.readouterr().err
    assert main(["llms", "missing.module:app"]) == 2
    assert "cannot load missing.module:app" in capsys.readouterr().err


async def test_the_page_is_off_unless_enabled(pool: Pool) -> None:
    client = await pool.start(make_app(openapi_url="/openapi.json"))
    assert (await client.request("GET", "/llms.txt")).status == 404


async def test_the_page_is_served_without_the_json_document(pool: Pool) -> None:
    client = await pool.start(make_app(llms_url="/llms.txt"))
    reply = await client.request("GET", "/llms.txt")
    assert reply.status == 200
    assert reply.headers["content-type"] == "text/markdown; charset=utf-8"
    assert reply.body == render_llms(make_app().openapi())
    assert "/ops/reindex" not in str(reply.body)
    assert (await client.request("GET", "/openapi.json")).status == 404


async def test_head_matches_get_without_a_body(pool: Pool) -> None:
    client = await pool.start(
        make_app(openapi_url="/openapi.json", llms_url="/llms.txt")
    )
    get = await client.request("GET", "/llms.txt")
    head = await client.request("HEAD", "/llms.txt")
    assert head.status == get.status == 200
    assert head.body is None
    assert head.headers["content-length"] == get.headers["content-length"]
    assert {k: v for k, v in head.headers.items() if k != "x-request-id"} == {
        k: v for k, v in get.headers.items() if k != "x-request-id"
    }
    assert "x-request-id" in head.headers
    assert (
        "/llms.txt" not in (await client.request("GET", "/openapi.json")).body["paths"]
    )


def test_the_argument_overrides_the_setting_and_none_disables_it() -> None:
    settings = AppSettings.load(prefix="X_", env={"X_DOCS__LLMS_URL": "/llms.txt"})
    assert Hinoki(settings=settings).llms_url == "/llms.txt"
    assert Hinoki(settings=settings, llms_url="/ai.txt").llms_url == "/ai.txt"
    assert Hinoki(settings=settings, llms_url=None).llms_url is None
    assert Hinoki().llms_url is None


def test_the_page_cannot_share_a_path_with_the_document() -> None:
    with pytest.raises(ValueError, match="llms_url must differ"):
        Hinoki(openapi_url="/api", llms_url="/api")


def test_output_writes_lf_even_when_text_mode_translates_newlines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def windows_write_text(self: Path, data: str, **_: object) -> int:
        return self.write_bytes(data.replace("\n", "\r\n").encode())

    monkeypatch.setattr(Path, "write_text", windows_write_text)
    output = tmp_path / "llms.txt"
    assert main(["llms", TARGET, "--output", str(output)]) == 0
    assert b"\r" not in output.read_bytes()
    assert main(["llms", TARGET, "--check", str(output)]) == 0


def test_check_accepts_a_crlf_checkout(tmp_path: Path) -> None:
    output = tmp_path / "llms.txt"
    assert main(["llms", TARGET, "--output", str(output)]) == 0
    output.write_bytes(output.read_bytes().replace(b"\n", b"\r\n"))
    assert main(["llms", TARGET, "--check", str(output)]) == 0


def test_an_operation_inherits_the_document_security_unless_it_overrides() -> None:
    document = {
        "info": {"title": "T", "version": "1"},
        "security": [{"Bearer": []}],
        "paths": {
            "/a": {"get": {"operationId": "a"}},
            "/b": {"get": {"operationId": "b", "security": []}},
            "/c": {"get": {"operationId": "c", "security": [{"Key": []}]}},
        },
    }
    rows = {
        row.split("`")[1]: row
        for row in render_llms(document).splitlines()
        if row.startswith("- `")
    }
    assert "security: Bearer" in rows["GET /a"]
    assert "security" not in rows["GET /b"]
    assert "security: Key" in rows["GET /c"]


def test_llms_url_must_start_with_a_slash() -> None:
    with pytest.raises(ValueError, match="must start with '/'"):
        Hinoki(llms_url="llms.txt")
    settings = AppSettings.load(prefix="X_", env={"X_DOCS__LLMS_URL": "llms.txt"})
    with pytest.raises(ValueError, match="must start with '/'"):
        Hinoki(settings=settings)
