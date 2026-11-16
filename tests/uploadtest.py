"""Typed, bounded multipart uploads through the public request and test APIs."""

import pytest
from openapi_spec_validator import validate

from hinoki import Access, Hinoki, MultiPartParser, Request, Schema, UploadedFile, http
from hinoki.errors import RegistrationError
from hinoki.parsers import Parsed
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


def test_file_binding_requires_multipart_and_rejects_ambiguous_signatures() -> None:
    @http.post("/missing-parser")
    def missing_parser(file: UploadedFile) -> None: ...

    @http.get("/wrong-method", parsers=[UPLOADS])
    def wrong_method(file: UploadedFile) -> None: ...

    @http.post("/{file}", parsers=[UPLOADS])
    def path_collision(file: UploadedFile) -> None: ...

    for handler in (missing_parser, wrong_method, path_collision, schema_and_file):
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
