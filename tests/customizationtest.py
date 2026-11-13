"""Parsers and renderers: Rust-based defaults that users can configure or subclass."""

import json
import random
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

from hinoki import HinokiAPI, Request, Schema, get, native, post
from hinoki.parsers import JSONParser, TextParser
from hinoki.renderers import DEFAULT_RENDERERS, JSONRenderer, TextRenderer
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


TEXT_CHARS = 'ab"\\\n\u00e9\U0001f600 '


class Dumper:
    def dump(self) -> dict[str, object]:
        return {"a": [1, {"b": 2}]}


def test_replacements_keep_the_indentation_of_the_value_they_replace() -> None:
    pretty = native.encode_json([Dumper()], 2)
    assert pretty == json.dumps([Dumper().dump()], indent=2).encode()
    hooked = native.encode_json(
        {"k": [Decimal("1")]}, 2, False, lambda v: {"d": str(v)}
    )
    assert hooked == json.dumps({"k": [{"d": "1"}]}, indent=2).encode()


def test_endless_replacements_and_absurd_indents_are_refused_cleanly() -> None:
    class Forever:
        def dump(self) -> object:
            return Forever()

    with pytest.raises(ValueError, match="deeply"):
        native.encode_json(Forever())
    with pytest.raises(ValueError, match="deeply"):
        native.encode_json(Decimal(1), None, False, lambda _v: Decimal(2))
    for bad in (257, 2**40):
        with pytest.raises(ValueError, match="indent"):
            native.encode_json([[1]], bad)
    with pytest.raises(OverflowError):
        native.encode_json([[1]], -1)


def test_random_trees_round_trip_with_every_option() -> None:
    rng = random.Random(7)  # noqa: S311 - a test fixture, not security

    def tree(depth: int) -> object:  # noqa: PLR0911 - one shape per random pick
        pick = rng.randrange(7 if depth else 4)
        if pick == 0:
            return rng.choice([None, True, False])
        if pick == 1:
            return rng.randint(-(10**20), 10**20)
        if pick == 2:
            return rng.random() * 1e6
        if pick == 3:
            return "".join(rng.choice('ab"\\\né\U0001f600 ') for _ in range(5))
        if pick in (4, 5):
            return [tree(depth - 1) for _ in range(rng.randrange(4))]
        return {
            f"k{rng.randrange(9)}{rng.choice('xé')}": tree(depth - 1) for _ in range(3)
        }

    for _ in range(300):
        value = tree(4)
        for indent in (None, 0, 2, 16):
            for sort in (False, True):
                assert json.loads(native.encode_json(value, indent, sort)) == value


class WithPostInit(Schema):
    x: int
    y: int = 5
    seen: bool = False  # set by __post_init__ below, never by the caller

    def __post_init__(self) -> None:
        object.__setattr__(self, "seen", True)


def test_from_model_still_runs_custom_initialisation() -> None:
    full = WithPostInit.from_model(SimpleNamespace(x=1, y=2, seen=False))
    partial = WithPostInit.from_model(SimpleNamespace(x=1))
    assert full.seen is True and partial.seen is True  # same behaviour either way


def test_explicit_default_renderers_are_not_overridden_by_application_defaults() -> (
    None
):
    @get("/chosen", renderers=DEFAULT_RENDERERS)
    async def chosen() -> dict[str, int]:
        return {"z": 1, "a": 2}

    @get("/unchosen")
    async def unchosen() -> dict[str, int]:
        return {"z": 1, "a": 2}

    app = HinokiAPI(renderers=[JSONRenderer(sort_keys=True)])
    app.include(chosen, unchosen)
    routes = {r.path: r for r in app.routes}
    assert routes["/chosen"].renderers[0].sort_keys is False  # type: ignore[attr-defined]
    assert routes["/unchosen"].renderers[0].sort_keys is True  # type: ignore[attr-defined]


async def test_a_buggy_prepare_hook_is_not_reported_as_invalid_json() -> None:
    class Broken(JSONParser):
        def prepare(self, data: object) -> object:
            raise ValueError("my bug")

    @post("/p", parsers=[Broken()])
    async def p(request: Request) -> dict[str, object]:
        return {"d": request.data}

    app = HinokiAPI()
    app.include(p)
    async with ASGIClient(app) as client:
        assert (await client.post("/p", json={"a": 1})).status == 500
        bad = await client.post(
            "/p", body=b"{", headers={"content-type": "application/json"}
        )
        assert (bad.status, bad.body["error"]) == (400, "invalid_json")
