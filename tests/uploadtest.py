"""Typed, bounded multipart uploads through the public request and test APIs."""

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from openapi_spec_validator import validate

from hinoki import (
    API,
    Access,
    Form,
    FormParser,
    Hinoki,
    Includable,
    JSONParser,
    MultiPartParser,
    Request,
    RouteGroup,
    Schema,
    UploadedFile,
    http,
)
from hinoki.errors import RegistrationError
from hinoki.parsers import (
    DEFAULT_PARSERS,
    DEFAULT_PART_BYTES,
    DEFAULT_PARTS,
    Parsed,
)
from hinoki.routing import Mount, route_of
from hinoki.runtime.config import RuntimeConfig
from hinoki.testing import ASGIClient, SyncClient
from tests.providers import TokenAuth

UPLOADS = MultiPartParser(max_parts=8, max_part_bytes=4)
BOUNDARY = "hinoki-test-boundary"


class Payload(Schema):
    text: str


class TrackingParser(MultiPartParser):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def parse(self, raw: bytes, content_type: str) -> Parsed:
        self.calls += 1
        return super().parse(raw, content_type)


PROTECTED_PARSER = TrackingParser()
PROTECTED_CALLS = 0


@http.post("/required", parsers=[UPLOADS])
def required(request: Request, file: UploadedFile) -> dict[str, object]:
    """Return uploaded-file metadata and the adjacent form field."""
    return {
        "same": request.FILES.getlist("file")[0] is file,
        "name": file.filename,
        "type": file.content_type,
        "size": file.size,
        "content": file.content.decode(),
        "title": request.POST.get("title"),
    }


@http.post("/batch", parsers=[UPLOADS])
def batch(files: list[UploadedFile]) -> list[str]:
    return [file.filename for file in files]


@http.post("/optional", parsers=[UPLOADS])
async def optional(
    request: Request, file: UploadedFile | None = None
) -> dict[str, object]:
    return {
        "filename": None if file is None else file.filename,
        "data": request.data,
        "files": len(request.FILES),
    }


@http.post(
    "/protected",
    access=Access(auth=TokenAuth(), permissions=["posts:write"]),
    parsers=[PROTECTED_PARSER],
)
def protected(file: UploadedFile) -> dict[str, str]:
    global PROTECTED_CALLS  # noqa: PLW0603 - measured by the denial regression
    PROTECTED_CALLS += 1
    return {"filename": file.filename}


@http.post("/schema-and-file", parsers=[UPLOADS])
def schema_and_file(data: Payload, file: UploadedFile) -> None:
    raise AssertionError("invalid registration")


@http.post("/default-parser")
def default_parser(file: UploadedFile) -> dict[str, str]:
    return {"filename": file.filename}


@http.post("/auto")
def auto_file(request: Request, file: UploadedFile) -> dict[str, object]:
    return {"filename": file.filename, "title": request.POST.get("title")}


@http.put("/auto-batch")
async def auto_batch(files: list[UploadedFile]) -> list[str]:
    return [file.filename for file in files]


@http.patch("/auto-optional")
async def auto_optional(file: UploadedFile | None = None) -> dict[str, object]:
    return {"filename": None if file is None else file.filename}


@http.post("/json-body")
def json_body(data: Payload) -> dict[str, str]:
    return {"text": data.text}


@http.post("/explicit-json", parsers=[JSONParser()])
def explicit_json(file: UploadedFile) -> None: ...


@http.post("/auto-schema-and-file")
def auto_schema_and_file(data: Payload, file: UploadedFile) -> None: ...


@http.get("/auto-get")
def auto_get(file: UploadedFile) -> None: ...


@http.post("/unresolved")
def unresolved(file: "MissingUpload") -> None:  # type: ignore[name-defined]  # noqa: F821
    raise AssertionError("invalid registration")


uploads_group = RouteGroup("/group")


@uploads_group.post("/files")
def group_file(file: UploadedFile) -> dict[str, str]:
    return {"filename": file.filename}


class Documents(API, path="/documents"):
    @http.post("/")
    async def create(self, document: UploadedFile) -> dict[str, str]:
        return {"filename": document.filename}


class ReplacedDocuments(Documents, path="/replaced"):
    @http.post("/")
    def create(self, document: UploadedFile, extra: UploadedFile) -> list[str]:  # type: ignore[override]
        return [document.filename, extra.filename]


class JSONDocuments(API, path="/json-documents"):
    @http.post("/", parsers=[JSONParser()])
    def create(self, data: Payload) -> dict[str, str]:
        return {"text": data.text}


class FileReplacement(JSONDocuments, path="/file-replacement"):
    @http.post("/")
    def create(self, document: UploadedFile) -> dict[str, str]:  # type: ignore[override]
        return {"filename": document.filename}


def parsers_of(application: Hinoki, path: str) -> tuple[object, ...]:
    return next(
        binding.route.parsers
        for binding in application.bindings
        if binding.route.path == path
    )


def assert_automatic_multipart(parsers: tuple[object, ...]) -> None:
    assert len(parsers) == 1
    parser = parsers[0]
    assert type(parser) is MultiPartParser
    assert parser.max_parts == DEFAULT_PARTS
    assert parser.max_part_bytes == DEFAULT_PART_BYTES


def app() -> Hinoki:
    return Hinoki(
        openapi_url="/openapi.json",
        include=[required, batch, optional, protected],
    )


def part(
    name: str,
    content: bytes,
    *,
    filename: str | None = None,
    content_type: str = "text/plain",
    extra_headers: tuple[tuple[str, str], ...] = (),
) -> bytes:
    disposition = f'form-data; name="{name}"'
    if filename is not None:
        disposition += f'; filename="{filename}"'
    headers = f"Content-Disposition: {disposition}\r\n"
    if filename is not None:
        headers += f"Content-Type: {content_type}\r\n"
    headers += "".join(f"{key}: {value}\r\n" for key, value in extra_headers)
    return f"--{BOUNDARY}\r\n{headers}\r\n".encode() + content + b"\r\n"


def multipart(*parts: bytes, boundary: str = BOUNDARY) -> bytes:
    return b"".join(parts) + f"--{boundary}--\r\n".encode()


async def test_typed_file_is_shared_with_request_and_form_fields() -> None:
    async with ASGIClient(app()) as client:
        response = await client.post(
            "/required",
            data={"title": "Rpt"},
            files=[("file", "report.txt", b"four", "text/plain")],
        )
    assert response.status == 200
    assert response.body == {
        "same": True,
        "name": "report.txt",
        "type": "text/plain",
        "size": 4,
        "content": "four",
        "title": "Rpt",
    }


async def test_repeated_files_keep_order_and_single_files_reject_duplicates() -> None:
    body = multipart(
        part("files", b"one", filename="one.txt"),
        part("files", b"two", filename="two.txt"),
    )
    async with ASGIClient(app()) as client:
        response = await client.post(
            "/batch",
            body=body,
            headers={"content-type": f"multipart/form-data; boundary={BOUNDARY}"},
        )
        duplicate = await client.post(
            "/required",
            files=[
                ("file", "one.txt", b"one", "text/plain"),
                ("file", "two.txt", b"two", "text/plain"),
            ],
        )
    assert (response.status, response.body) == (200, ["one.txt", "two.txt"])
    assert (duplicate.status, duplicate.body["error"]) == (422, "validation_error")
    assert duplicate.body["details"][0]["code"] == "duplicate"


async def test_optional_and_required_empty_upload_semantics() -> None:
    async with ASGIClient(app()) as client:
        required_empty = await client.post("/required")
        optional_empty = await client.post("/optional")
        valid_empty_multipart = await client.post(
            "/optional",
            body=f"--{BOUNDARY}--\r\n".encode(),
            headers={"content-type": f"multipart/form-data; boundary={BOUNDARY}"},
        )
        malformed_empty_multipart = await client.post(
            "/optional",
            body=b"",
            headers={"content-type": f"multipart/form-data; boundary={BOUNDARY}"},
        )
    assert (required_empty.status, required_empty.body["error"]) == (
        422,
        "validation_error",
    )
    assert (optional_empty.status, optional_empty.body) == (
        200,
        {"filename": None, "data": {}, "files": 0},
    )
    assert (valid_empty_multipart.status, valid_empty_multipart.body) == (
        200,
        {"filename": None, "data": {}, "files": 0},
    )
    assert (
        malformed_empty_multipart.status,
        malformed_empty_multipart.body["error"],
    ) == (
        400,
        "invalid_multipart",
    )


async def test_wrong_media_type_and_untyped_nonempty_body_are_refused() -> None:
    async with ASGIClient(app()) as client:
        wrong = await client.post("/required", json={"file": "not a file"})
        no_type = await client.post("/required", body=b"not multipart")
    assert (wrong.status, wrong.body["error"]) == (415, "unsupported_media_type")
    assert (no_type.status, no_type.body["error"]) == (415, "unsupported_media_type")


async def test_part_size_limit_is_inclusive_and_overflow_is_413() -> None:
    async with ASGIClient(app()) as client:
        exact = await client.post(
            "/required", files=[("file", "four.txt", b"four", "text/plain")]
        )
        overflow = await client.post(
            "/required", files=[("file", "five.txt", b"fives", "text/plain")]
        )
    assert exact.status == 200
    assert (overflow.status, overflow.body["error"]) == (413, "payload_too_large")


async def test_total_body_limit_accepts_exact_cap_and_rejects_one_below() -> None:
    body = multipart(part("file", b"four", filename="four.txt"))
    headers = {"content-type": f"multipart/form-data; boundary={BOUNDARY}"}
    exact_app = Hinoki(
        runtime=RuntimeConfig(max_body_bytes=len(body)),
        include=[required],
    )
    small_app = Hinoki(
        runtime=RuntimeConfig(max_body_bytes=len(body) - 1),
        include=[required],
    )
    async with ASGIClient(exact_app) as client:
        exact = await client.post("/required", body=body, headers=headers)
    async with ASGIClient(small_app) as client:
        overflow = await client.post("/required", body=body, headers=headers)
    assert exact.status == 200
    assert (overflow.status, overflow.body["error"]) == (413, "payload_too_large")


async def test_duplicate_part_content_type_is_a_bad_request() -> None:
    duplicate = part(
        "file",
        b"one",
        filename="one.txt",
        extra_headers=(("content-type", "application/octet-stream"),),
    )
    body = multipart(duplicate)
    async with ASGIClient(app()) as client:
        response = await client.post(
            "/required",
            body=body,
            headers={"content-type": f"multipart/form-data; boundary={BOUNDARY}"},
        )
    assert (response.status, response.body["error"]) == (400, "invalid_multipart")


async def test_access_denial_does_not_parse_or_invoke_uploaded_content() -> None:
    global PROTECTED_CALLS  # noqa: PLW0603 - reset the observed handler count
    PROTECTED_CALLS = 0
    PROTECTED_PARSER.calls = 0
    async with ASGIClient(app()) as client:
        response = await client.post(
            "/protected",
            body=b"malformed private upload",
            headers={"content-type": f"multipart/form-data; boundary={BOUNDARY}"},
        )
    assert response.status == 401
    assert PROTECTED_PARSER.calls == 0
    assert PROTECTED_CALLS == 0


async def test_openapi_documents_binary_files_and_optional_request_body() -> None:
    document = app().openapi()
    validate(document)
    paths = document["paths"]
    required_body = paths["/required"]["post"]["requestBody"]
    optional_body = paths["/optional"]["post"]["requestBody"]
    batch_schema = paths["/batch"]["post"]["requestBody"]["content"][
        "multipart/form-data"
    ]["schema"]
    assert required_body["required"] is True
    assert required_body["content"]["multipart/form-data"]["schema"]["required"] == [
        "file"
    ]
    assert optional_body["required"] is False
    assert "required" not in optional_body["content"]["multipart/form-data"]["schema"]
    assert batch_schema["properties"]["files"] == {
        "type": "array",
        "items": {"type": "string", "format": "binary"},
    }


def test_file_binding_rejects_ambiguous_signatures() -> None:
    @http.get("/wrong-method", parsers=[UPLOADS])
    def wrong_method(file: UploadedFile) -> None: ...

    @http.post("/{file}", parsers=[UPLOADS])
    def path_collision(file: UploadedFile) -> None: ...

    for handler in (wrong_method, path_collision, schema_and_file):
        with pytest.raises(RegistrationError):
            Hinoki().include(handler)


async def test_application_default_parser_can_supply_multipart_support() -> None:
    default_app = Hinoki(parsers=[UPLOADS], include=[default_parser])
    async with ASGIClient(default_app) as client:
        response = await client.post(
            "/default-parser",
            files=[("file", "default.txt", b"file", "text/plain")],
        )
    assert (response.status, response.body) == (200, {"filename": "default.txt"})


async def test_client_rejects_conflicting_multipart_inputs() -> None:
    async with ASGIClient(app()) as client:
        with pytest.raises(ValueError, match="cannot be combined"):
            await client.post(
                "/required",
                json={"file": "x"},
                files=[("file", "x.txt", b"x", "text/plain")],
            )
        with pytest.raises(ValueError, match="cannot be combined"):
            await client.post(
                "/required",
                body=b"raw",
                data={"title": "x"},
            )
        with pytest.raises(ValueError, match="set by the multipart"):
            await client.post(
                "/required",
                files=[("file", "x.txt", b"x", "text/plain")],
                headers={"content-type": "multipart/form-data"},
            )


def test_sync_client_can_send_files() -> None:
    with SyncClient(app()) as client:
        response = client.post(
            "/required", files=[("file", "sync.txt", b"sync", "text/plain")]
        )
    assert response.body["name"] == "sync.txt"


AUTOMATIC: list[Includable] = [auto_file, auto_batch, auto_optional, json_body]


async def test_upload_actions_without_parsers_select_multipart_automatically() -> None:
    automatic = Hinoki(openapi_url="/openapi.json", include=AUTOMATIC)
    for path in ("/auto", "/auto-batch", "/auto-optional"):
        assert_automatic_multipart(parsers_of(automatic, path))
    assert parsers_of(automatic, "/json-body") is DEFAULT_PARSERS
    original = route_of(auto_file)
    assert original is not None
    assert original.parsers is DEFAULT_PARSERS
    async with ASGIClient(automatic) as client:
        single = await client.post(
            "/auto",
            data={"title": "Report"},
            files=[("file", "a.txt", b"a", "text/plain")],
        )
        many = await client.put(
            "/auto-batch",
            files=[
                ("files", "one.txt", b"1", "text/plain"),
                ("files", "two.txt", b"2", "text/plain"),
            ],
        )
        absent = await client.patch("/auto-optional")
        wrong_type = await client.post("/auto", json={"file": "a.txt"})
        body = await client.post("/json-body", json={"text": "hi"})
    assert (single.status, single.body) == (
        200,
        {"filename": "a.txt", "title": "Report"},
    )
    assert (many.status, many.body) == (200, ["one.txt", "two.txt"])
    assert (absent.status, absent.body) == (200, {"filename": None})
    assert wrong_type.status == 415
    assert (body.status, body.body) == (200, {"text": "hi"})
    document = automatic.openapi()
    validate(document)
    content = document["paths"]["/auto"]["post"]["requestBody"]["content"]
    assert list(content) == ["multipart/form-data"]
    assert list(document["paths"]["/json-body"]["post"]["requestBody"]["content"]) == [
        "application/json"
    ]


def test_sync_client_uploads_to_an_automatic_multipart_action() -> None:
    with SyncClient(Hinoki(include=[auto_file])) as client:
        response = client.post("/auto", files=[("file", "s.txt", b"s", "text/plain")])
    assert (response.status, response.body) == (
        200,
        {"filename": "s.txt", "title": None},
    )


def test_application_parsers_keep_precedence_when_they_include_multipart() -> None:
    with_multipart = Hinoki(parsers=[JSONParser(), UPLOADS], include=AUTOMATIC)
    assert parsers_of(with_multipart, "/auto")[1] is UPLOADS
    assert parsers_of(with_multipart, "/json-body")[1] is UPLOADS
    without_multipart = Hinoki(parsers=[JSONParser(), FormParser()], include=AUTOMATIC)
    assert_automatic_multipart(parsers_of(without_multipart, "/auto"))
    json_parsers = parsers_of(without_multipart, "/json-body")
    assert [type(parser) for parser in json_parsers] == [JSONParser, FormParser]
    explicit = Hinoki(parsers=[MultiPartParser()], include=[required])
    assert parsers_of(explicit, "/required") == (UPLOADS,)


def test_explicit_action_parsers_without_multipart_still_fail() -> None:
    with pytest.raises(RegistrationError, match="omit parsers= to select it"):
        Hinoki(include=[explicit_json])
    with pytest.raises(RegistrationError, match="omit parsers= to select it"):
        Hinoki(parsers=[UPLOADS], include=[explicit_json])


def test_automatic_selection_keeps_the_original_registration_errors() -> None:
    with pytest.raises(
        RegistrationError,
        match=r"next to file parameters must be declared Form\[\.\.\.\]",
    ):
        Hinoki(include=[auto_schema_and_file])
    with pytest.raises(RegistrationError, match="requires a POST/PUT/PATCH/QUERY"):
        Hinoki(include=[auto_get])
    with pytest.raises(RegistrationError, match="cannot resolve annotations"):
        Hinoki(include=[unresolved])


async def test_groups_mounts_and_api_classes_select_multipart_automatically() -> None:
    application = Hinoki(
        include=[
            uploads_group,
            Mount("/v1", auto_file),
            Documents,
            ReplacedDocuments,
            FileReplacement,
        ]
    )
    for path in ("/group/files", "/v1/auto", "/documents", "/replaced"):
        assert_automatic_multipart(parsers_of(application, path))
    async with ASGIClient(application) as client:
        grouped = await client.post(
            "/group/files", files=[("file", "g.txt", b"g", "text/plain")]
        )
        replaced = await client.post(
            "/replaced",
            files=[
                ("document", "d.txt", b"d", "text/plain"),
                ("extra", "e.txt", b"e", "text/plain"),
            ],
        )
    assert (grouped.status, grouped.body) == (200, {"filename": "g.txt"})
    assert (replaced.status, replaced.body) == (200, ["d.txt", "e.txt"])


def test_a_replacement_does_not_inherit_the_replaced_parsers() -> None:
    application = Hinoki(include=[FileReplacement])
    assert_automatic_multipart(parsers_of(application, "/file-replacement"))


async def test_application_and_automatic_multipart_limits_apply_at_runtime() -> None:
    application = Hinoki(parsers=[JSONParser(), UPLOADS], include=[auto_file])
    automatic = Hinoki(include=[auto_batch])
    too_many = [
        ("files", f"{index}.txt", b"x", "text/plain")
        for index in range(DEFAULT_PARTS + 1)
    ]
    async with ASGIClient(application) as client:
        fits = await client.post(
            "/auto", files=[("file", "a.txt", b"four", "text/plain")]
        )
        oversized = await client.post(
            "/auto", files=[("file", "a.txt", b"fives", "text/plain")]
        )
    async with ASGIClient(automatic) as client:
        at_limit = await client.put("/auto-batch", files=too_many[:-1])
        parts = await client.put("/auto-batch", files=too_many)
    assert fits.status == 200
    assert (oversized.status, oversized.body["error"]) == (413, "payload_too_large")
    assert (at_limit.status, len(at_limit.body)) == (200, DEFAULT_PARTS)
    assert (parts.status, parts.body["error"]) == (400, "invalid_multipart")


class Upload(Schema):
    title: str
    pages: int


@http.post("/form-and-file")
async def form_and_file(
    meta: Form[Upload], file: UploadedFile, extra: UploadedFile | None = None
) -> dict[str, object]:
    return {
        "title": meta.title,
        "pages": meta.pages,
        "file": file.filename,
        "extra": extra is not None,
    }


async def test_a_form_schema_binds_the_text_fields_next_to_the_files() -> None:
    application = Hinoki(include=[form_and_file])
    async with ASGIClient(application) as client:
        sent = await client.post(
            "/form-and-file",
            data={"title": "Report", "pages": "3"},
            files=[("file", "r.txt", b"r", "text/plain")],
        )
        invalid = await client.post(
            "/form-and-file",
            data={"title": "Report", "pages": "many"},
            files=[("file", "r.txt", b"r", "text/plain")],
        )
        empty = await client.post("/form-and-file")
    assert (sent.status, sent.body) == (
        200,
        {"title": "Report", "pages": 3, "file": "r.txt", "extra": False},
    )
    assert invalid.status == 422
    assert invalid.body["details"][0]["path"] == "meta.pages"
    assert empty.status == 422


def test_the_openapi_body_joins_the_form_schema_and_the_files() -> None:
    document = Hinoki(include=[form_and_file]).openapi()
    validate(document)
    body = document["paths"]["/form-and-file"]["post"]["requestBody"]
    schema = body["content"]["multipart/form-data"]["schema"]
    assert body["required"] is True
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"title", "pages", "file", "extra"}
    assert schema["required"] == ["title", "pages", "file"]
    payload = {"title": "Report", "pages": 3, "file": "r"}
    assert Draft202012Validator(schema).is_valid(payload)
    assert not Draft202012Validator(schema).is_valid({**payload, "other": 1})
