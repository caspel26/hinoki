"""``Field(pattern=...)`` on a ``bytes`` field: the decoded bytes are read as
Latin-1 characters (one character per byte) and matched as a whole, the same
way on the Python-object lane, the native JSON lane and direct construction."""

from __future__ import annotations

import base64
import re
from typing import Annotated

import pytest

from hinoki import Field, Hinoki, Schema, http
from hinoki.errors import RegistrationError, ValidationError
from hinoki.runtime.kiso import KisoWorker
from hinoki.testing import ASGIClient
from tests.kisoservertest import JSON, fetch
from tests.xmlnativelanetest import recording

PNG = rb"\x89PNG[\x00-\xff]*"


class Upload(Schema):
    data: Annotated[bytes, Field(pattern=PNG.decode(), max_length=64)]
    tag: Annotated[bytes, Field(pattern="[a-z]{2}\\xe9?")] = b"ab"


@http.post("/upload")
async def upload(body: Upload) -> Upload:
    return body


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def test_validate_matches_the_bytes_as_latin_1() -> None:
    value = Upload.validate(
        {"data": b64(b"\x89PNG\r\n\x00\xff"), "tag": b64(b"ab\xe9")}
    )
    assert value.data == b"\x89PNG\r\n\x00\xff"
    assert value.tag == b"ab\xe9"


def test_a_mismatch_is_reported_with_the_pattern_code() -> None:
    with pytest.raises(ValidationError) as caught:
        Upload.validate({"data": b64(b"GIF89a"), "tag": b64(b"ab\xe8")})
    assert [(d.path, d.code) for d in caught.value.details] == [
        ("data", "pattern"),
        ("tag", "pattern"),
    ]


def test_the_match_covers_the_whole_value() -> None:
    with pytest.raises(ValidationError):
        Upload.validate({"data": b64(b"\x89PNG"), "tag": b64(b"abc")})


def test_direct_construction_checks_the_pattern() -> None:
    assert Upload(data=b"\x89PNG").tag == b"ab"
    with pytest.raises(ValidationError):
        Upload(data=b"nope")


def test_latin_1_agrees_with_python_re_for_every_byte() -> None:
    class Low(Schema):
        data: Annotated[bytes, Field(pattern="[\\x00-\\x7f]*")]

    for byte in range(256):
        raw = bytes([byte])
        expected = re.fullmatch("[\x00-\x7f]*", raw.decode("latin-1")) is not None
        try:
            Low.validate({"data": b64(raw)})
            accepted = True
        except ValidationError:
            accepted = False
        assert accepted is expected, byte


def test_a_bad_pattern_is_refused_at_registration() -> None:
    with pytest.raises(RegistrationError):

        class Bad(Schema):
            data: Annotated[bytes, Field(pattern="(?=a)a")]


async def test_a_request_body_is_checked() -> None:
    async with ASGIClient(Hinoki(include=[upload])) as client:
        good = await client.post("/upload", json={"data": b64(b"\x89PNGx")})
        bad = await client.post("/upload", json={"data": b64(b"GIF")})
    assert good.status == 200, good.body
    assert bad.status == 422
    assert [d["code"] for d in bad.body["details"]] == ["pattern"]


BODIES = [
    b'{"data": "iVBORw==", "tag": "YWLp"}',
    b'{"data": "R0lG", "tag": "YWLo"}',
    b'{"data": "iVBORw==", "tag": "YQ=="}',
]


async def test_the_native_lane_matches_like_the_python_lane() -> None:
    app = Hinoki(include=[upload])
    worker = KisoWorker(app, "127.0.0.1", 0, native_dispatch=False)
    lanes: list[str] = []
    pipeline = worker.pipeline
    pipeline.handle_native = recording(lanes, "native", pipeline.handle_native)  # type: ignore[method-assign]
    pipeline.handle_unfinished = recording(lanes, "python", pipeline.handle_unfinished)  # type: ignore[method-assign]
    port = await worker.start()
    try:
        native = [
            await fetch(
                port,
                "POST",
                "/upload",
                JSON + f"content-length: {len(body)}\r\n",
                body=body,
            )
            for body in BODIES
        ]
    finally:
        await worker.stop()
    assert lanes == ["native"] * len(BODIES)
    async with ASGIClient(app) as client:
        for body, got in zip(BODIES, native, strict=True):
            expected = await client.post(
                "/upload", body=body, headers={"content-type": "application/json"}
            )
            assert got.status == expected.status
            if got.status == 200:
                assert got.body == expected.raw
            else:
                assert got.json()["details"] == expected.body["details"]
    assert [got.status for got in native] == [200, 422, 422]


def test_the_openapi_document_has_no_pattern_for_bytes() -> None:
    schema = Hinoki(include=[upload]).openapi()["components"]["schemas"]["Upload"]
    assert "pattern" not in schema["properties"]["data"]
