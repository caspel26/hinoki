"""Parsers and renderers: Rust-based defaults that users can configure or subclass."""

import json
from decimal import Decimal
from uuid import UUID

import pytest

from hinoki import HinokiAPI, Request, Schema, get, native, post
from hinoki.parsers import JSONParser, TextParser
from hinoki.renderers import JSONRenderer, TextRenderer
from hinoki.testing import ASGIClient


class Item(Schema):
    id: UUID
    name: str


def test_the_rust_encoder_has_indent_sort_keys_and_a_default_hook() -> None:
    value = {"b": [1, {"y": 2, "x": None}], "a": "é"}
    assert native.encode_json(value) == b'{"b":[1,{"y":2,"x":null}],"a":"\xc3\xa9"}'
    assert native.encode_json(value, None, True) == (
        b'{"a":"\xc3\xa9","b":[1,{"x":null,"y":2}]}'
    )
    assert native.encode_json({"k": [1, 2], "e": {}, "l": []}, 2) == (
        b'{\n  "k": [\n    1,\n    2\n  ],\n  "e": {},\n  "l": []\n}'
    )
    assert json.loads(native.encode_json(value, 4, True)) == value
    assert native.encode_json({"d": Decimal("1.5")}, None, False, str) == b'{"d":"1.5"}'
    with pytest.raises(TypeError):
        native.encode_json({"d": Decimal("1.5")})
    with pytest.raises(ValueError, match="same object"):
        native.encode_json(Decimal("1"), None, False, lambda v: v)
    item = Item(id=UUID(int=1), name="n")
    assert native.encode_json(item, None, True).startswith(b'{"id":')
    assert json.loads(native.encode_json([item], 2)) == [item.dump()]


def test_renderer_options_are_validated() -> None:
    assert JSONRenderer(indent=2, sort_keys=True).indent == 2
    with pytest.raises(ValueError, match="indent"):
        JSONRenderer(indent=-1)
    with pytest.raises(ValueError, match="indent"):
        JSONRenderer(indent=99)


class Envelope(JSONRenderer):
    media_type = "application/vnd.hinoki+json"

    def prepare(self, value: object, request: Request) -> object:
        return {"data": value, "path": request.path}


class SnakeIn(JSONParser):
    media_type = "application/json"

    def prepare(self, data: object) -> object:
        if isinstance(data, dict):
            return {k.replace("-", "_"): v for k, v in data.items()}
        return data


@get("/pretty", renderers=[JSONRenderer(indent=2, sort_keys=True)])
async def pretty() -> dict[str, int]:
    return {"b": 1, "a": 2}


@get("/wrapped", renderers=[Envelope()])
async def wrapped(request: Request) -> dict[str, int]:
    return {"n": 1}


@get("/money", renderers=[JSONRenderer(default=str)])
async def money() -> dict[str, Decimal]:
    return {"price": Decimal("9.50")}


@post("/echo", parsers=[SnakeIn(), TextParser()])
async def echo(request: Request) -> dict[str, object]:
    return {"got": request.data}


@get("/text", renderers=[TextRenderer()])
async def text() -> str:
    return "plain ✓"


async def test_customised_and_subclassed_codecs_work_through_the_stack() -> None:
    app = HinokiAPI()
    app.include(pretty, wrapped, money, echo, text)
    async with ASGIClient(app) as client:
        pretty_reply = await client.get("/pretty")
        assert pretty_reply.headers["content-type"].startswith("application/json")
        assert pretty_reply.raw == b'{\n  "a": 2,\n  "b": 1\n}'
        assert (await client.get("/wrapped", headers={"accept": "*/*"})).body == {
            "data": {"n": 1},
            "path": "/wrapped",
        }
        assert (await client.get("/money")).body == {"price": "9.50"}
        got = await client.post("/echo", json={"user-name": "a"})
        assert got.body == {"got": {"user_name": "a"}}
        said = await client.post(
            "/echo", body="héllo".encode(), headers={"content-type": "text/plain"}
        )
        assert said.body == {"got": "héllo"}
        bad = await client.post(
            "/echo", body=b"\xff", headers={"content-type": "text/plain"}
        )
        assert (bad.status, bad.body["error"]) == (400, "invalid_text")
        assert (await client.get("/text")).body == "plain ✓"


async def test_application_wide_defaults_apply_unless_an_action_chooses() -> None:
    @get("/plain")
    async def plain() -> dict[str, int]:
        return {"z": 1, "a": 2}

    app = HinokiAPI(renderers=[JSONRenderer(sort_keys=True), TextRenderer()])
    app.include(plain, text)
    async with ASGIClient(app) as client:
        reply = await client.get("/plain")
        assert list(reply.body) == ["a", "z"]  # the application default sorts keys
        assert (await client.get("/text")).body == "plain ✓"  # the action's own choice
    with pytest.raises(ValueError, match="distinct media types"):
        HinokiAPI(renderers=[JSONRenderer(), JSONRenderer()])
