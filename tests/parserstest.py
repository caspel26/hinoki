"""Body parsers: JSON by default, forms and multipart on request."""

from typing import Any

import pytest
from openapi_spec_validator import validate

from hinoki import (
    FormParser,
    Hinoki,
    JSONParser,
    MultiPartParser,
    Parser,
    Request,
    Schema,
    post,
)
from hinoki.errors import InvalidBody
from hinoki.parsers import Parsed
from tests.harness import Client, Pool

FORM = "application/x-www-form-urlencoded"
BOUNDARY = "xBOUNDARYx"
MULTIPART = f"multipart/form-data; boundary={BOUNDARY}"


class Note(Schema):
    title: str


@post("/json")
def plain(request: Request) -> dict[str, Any]:
    return {"data": request.data}


@post("/note", parsers=[JSONParser(), FormParser()])
def note(request: Request, data: Note) -> dict[str, str]:
    return {"title": data.title}


@post("/form", parsers=[FormParser()])
def form(request: Request) -> dict[str, Any]:
    return {
        "data": request.data,
        "all": request.POST.getlist("tag"),
        "last": request.POST["tag"],
        "files": len(request.FILES),
    }


@post("/upload", parsers=[MultiPartParser(max_parts=3, max_part_bytes=20)])
def upload(request: Request) -> dict[str, Any]:
    files = request.FILES.getlist("doc")
    return {
        "title": request.POST.get("title"),
        "files": [
            (f.filename, f.content_type, f.size, f.content.decode()) for f in files
        ],
    }


class CsvParser(Parser):
    media_type = "text/csv"

    def parse(self, raw: bytes, content_type: str) -> Parsed:
        if not raw:
            raise InvalidBody("invalid_csv", "empty")
        return Parsed([line.split(",") for line in raw.decode().splitlines()])


@post("/csv", parsers=[CsvParser()])
def csv(request: Request) -> dict[str, Any]:
    return {"rows": request.data}


def app() -> Hinoki:
    result = Hinoki(openapi_url="/openapi.json")
    result.include(plain, note, form, upload, csv)
    return result


async def started(pool: Pool) -> Client:
    return await pool.start(app())


def multipart(*parts: tuple[str, str | None, str]) -> bytes:
    out = b""
    for name, filename, content in parts:
        disposition = f'form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        kind = "Content-Type: text/plain\r\n" if filename is not None else ""
        out += (
            f"--{BOUNDARY}\r\nContent-Disposition: {disposition}\r\n{kind}\r\n"
            f"{content}\r\n"
        ).encode()
    return out + f"--{BOUNDARY}--\r\n".encode()


async def test_json_is_the_default_and_other_types_are_refused(pool: Pool) -> None:
    client = await started(pool)
    ok = await client.request(
        "POST", "/json", b'{"a": 1}', content_type="application/json"
    )
    assert ok.body == {"data": {"a": 1}}
    wrong = await client.request("POST", "/json", b"a=1", content_type=FORM)
    assert wrong.status == 415
    assert "application/json" in wrong.body["message"]
    broken = await client.request(
        "POST", "/json", b"{", content_type="application/json"
    )
    assert (broken.status, broken.body["error"]) == (400, "invalid_json")


async def test_a_schema_body_can_come_from_json_or_a_form(pool: Pool) -> None:
    client = await started(pool)
    as_json = await client.request(
        "POST", "/note", b'{"title": "j"}', content_type="application/json"
    )
    as_form = await client.request("POST", "/note", b"title=f", content_type=FORM)
    assert (as_json.body, as_form.body) == ({"title": "j"}, {"title": "f"})
    missing = await client.request("POST", "/note", b"other=1", content_type=FORM)
    assert missing.status == 422


async def test_form_fields_repeat_and_decode(pool: Pool) -> None:
    client = await started(pool)
    reply = await client.request(
        "POST", "/form", b"tag=a+b&tag=%C3%A9&x=", content_type=FORM
    )
    assert reply.body == {
        "data": {"tag": "é", "x": ""},
        "all": ["a b", "é"],
        "last": "é",
        "files": 0,
    }
    bad = await client.request("POST", "/form", b"tag=%ff", content_type=FORM)
    assert (bad.status, bad.body["error"]) == (400, "invalid_form")


async def test_multipart_fields_and_files(pool: Pool) -> None:
    client = await started(pool)
    body = multipart(
        ("title", None, "Hi"), ("doc", "a.txt", "one"), ("doc", "b.txt", "two")
    )
    reply = await client.request("POST", "/upload", body, content_type=MULTIPART)
    assert reply.body == {
        "title": "Hi",
        "files": [["a.txt", "text/plain", 3, "one"], ["b.txt", "text/plain", 3, "two"]],
    }


@pytest.mark.parametrize(
    "body",
    [
        b"not multipart",
        multipart(
            ("a", None, "1"), ("b", None, "2"), ("c", None, "3"), ("d", None, "4")
        ),
        multipart(("doc", "big.txt", "x" * 21)),
        multipart(("title", None, "ok"))[:-12],
    ],
)
async def test_malformed_or_oversized_multipart_is_a_400(
    pool: Pool, body: bytes
) -> None:
    client = await started(pool)
    reply = await client.request("POST", "/upload", body, content_type=MULTIPART)
    assert (reply.status, reply.body["error"]) == (400, "invalid_multipart")


async def test_a_missing_boundary_is_a_400(pool: Pool) -> None:
    client = await started(pool)
    reply = await client.request(
        "POST", "/upload", b"--x--", content_type="multipart/form-data"
    )
    assert (reply.status, reply.body["error"]) == (400, "invalid_multipart")


async def test_custom_parsers_plug_in(pool: Pool) -> None:
    client = await started(pool)
    reply = await client.request("POST", "/csv", b"a,b\n1,2", content_type="text/csv")
    assert reply.body == {"rows": [["a", "b"], ["1", "2"]]}
    empty = await client.request("POST", "/csv", b"", content_type="text/csv")
    assert (empty.status, empty.body["error"]) == (400, "invalid_csv")


async def test_form_accessors_are_empty_for_json(pool: Pool) -> None:
    @post("/probe")
    def probe(request: Request) -> dict[str, Any]:
        return {"post": dict(request.POST), "files": len(request.FILES)}

    application = Hinoki()
    application.include(probe)
    client = await pool.start(application)
    reply = await client.request(
        "POST", "/probe", b"{}", content_type="application/json"
    )
    assert reply.body == {"post": {}, "files": 0}


def test_openapi_lists_the_media_types_of_the_parsers() -> None:
    document = app().openapi()
    validate(document)
    content = document["paths"]["/note"]["post"]["requestBody"]["content"]
    assert set(content) == {"application/json", FORM}


@pytest.mark.parametrize("parsers", [[], [JSONParser(), JSONParser()], [Parser()]])
def test_parser_lists_are_checked_at_declaration(parsers: list[Parser]) -> None:
    with pytest.raises(ValueError, match="parsers must be"):
        post("/x", parsers=parsers)(len)


def test_multipart_limits_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        MultiPartParser(max_parts=0)
