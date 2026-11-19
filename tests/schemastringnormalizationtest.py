"""``Field(strip=True)``, ``Field(lower=True)`` and ``Field(upper=True)``: a
``str`` field is normalised before its length, pattern and choices are checked,
the same way on the Python-object lane and on the native JSON lane."""

from __future__ import annotations

from typing import Annotated, Any

import pytest

from hinoki import Field, Hinoki, Schema, field_normalize, http
from hinoki.errors import RegistrationError, ValidationError
from hinoki.runtime.kiso import KisoWorker
from hinoki.testing import ASGIClient
from tests.kisoservertest import JSON, fetch
from tests.xmlnativelanetest import recording


class Signup(Schema):
    handle: Annotated[
        str, Field(strip=True, lower=True, min_length=3, pattern="[a-z]+")
    ]
    code: Annotated[str, Field(upper=True, one_of=("AB", "CD"))]
    note: Annotated[str, Field(strip=True)] = ""


@http.post("/normalised")
async def signup(data: Signup) -> Signup:
    return data


def test_validate_normalises_before_the_constraints() -> None:
    value = Signup.validate(
        {"handle": "  AbC \t", "code": "ab", "note": "\x1c hi \u00a0"}
    )
    assert (value.handle, value.code, value.note) == ("abc", "AB", "hi")


def test_the_constraints_see_the_normalised_value() -> None:
    with pytest.raises(ValidationError) as caught:
        Signup.validate({"handle": "  ab  ", "code": "xx"})
    assert [(d.path, d.code) for d in caught.value.details] == [
        ("handle", "min_length"),
        ("code", "one_of"),
    ]
    with pytest.raises(ValidationError) as caught:
        Signup.validate({"handle": "a1b", "code": "ab"})
    assert [d.code for d in caught.value.details] == ["pattern"]


def test_direct_construction_normalises_too() -> None:
    value = Signup(handle=" ZED ", code="cd")
    assert (value.handle, value.code) == ("zed", "CD")


def test_unicode_case_mapping_matches_python() -> None:
    class Cased(Schema):
        low: Annotated[str, Field(lower=True)]
        up: Annotated[str, Field(upper=True)]

    text = "Straße İΣ ΑΣ"
    value = Cased.validate({"low": text, "up": text})
    assert (value.low, value.up) == (text.lower(), text.upper())


def test_only_a_str_field_may_normalise() -> None:
    with pytest.raises(RegistrationError):

        class OnInt(Schema):
            count: Annotated[int, Field(strip=True)]

    with pytest.raises(RegistrationError):

        class OnBytes(Schema):
            blob: Annotated[bytes, Field(lower=True)]


def test_lower_and_upper_exclude_each_other() -> None:
    with pytest.raises(RegistrationError, match=r"lower and upper"):

        class Both(Schema):
            name: Annotated[str, Field(lower=True, upper=True)]


def test_a_choice_the_normalisation_can_never_produce_is_refused() -> None:
    with pytest.raises(RegistrationError, match="can never match"):

        class Never(Schema):
            code: Annotated[str, Field(upper=True, one_of=("ab",))]


class Tag(Schema):
    label: Annotated[str, Field(strip=True)]

    @field_normalize("label")
    def shout(self, value: str) -> str:
        return f"{value}!"


class Post(Schema):
    tags: list[Tag]


def test_nested_and_custom_normalisation_compose() -> None:
    assert Post.validate({"tags": [{"label": " a "}]}).tags[0].label == "a!"
    assert Tag(label=" b ").label == "b!"


async def test_a_request_body_is_normalised() -> None:
    async with ASGIClient(Hinoki(include=[signup])) as client:
        reply = await client.post(
            "/normalised", json={"handle": " Bob ", "code": "cd", "note": " x "}
        )
    assert reply.status == 200, reply.body
    assert reply.body == {"handle": "bob", "code": "CD", "note": "x"}


BODIES = [
    b'{"handle": "  AbC ", "code": "ab", "note": "\\u2003 hi \\u0085"}',
    b'{"handle": "  ab  ", "code": "xx"}',
    b'{"handle": "Stra\\u00dfe", "code": "ab"}',
    b'{"handle": "\\u0130\\u03a3", "code": "cd", "note": "\\u001f\\u001e"}',
]


async def test_the_native_lane_normalises_like_the_python_lane() -> None:
    app = Hinoki(include=[signup])
    worker = KisoWorker(app, "127.0.0.1", 0)
    lanes: list[str] = []
    pipeline = worker.pipeline
    pipeline.handle_native = recording(lanes, "native", pipeline.handle_native)  # type: ignore[method-assign]
    pipeline.handle = recording(lanes, "python", pipeline.handle)  # type: ignore[method-assign]
    port = await worker.start()
    try:
        native = [
            await fetch(
                port,
                "POST",
                "/normalised",
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
                "/normalised", body=body, headers={"content-type": "application/json"}
            )
            assert got.status == expected.status
            if got.status == 200:
                assert got.body == expected.raw
            else:
                assert got.json()["details"] == expected.body["details"]
    assert native[0].json() == {"handle": "abc", "code": "AB", "note": "hi"}


def test_the_openapi_document_is_unchanged_by_normalisation() -> None:
    plain: dict[str, Any] = {"type": "string"}
    schema = Hinoki(include=[signup]).openapi()["components"]["schemas"]["Signup"]
    assert schema["properties"]["note"] == plain
    assert schema["properties"]["code"]["enum"] == ["AB", "CD"]
