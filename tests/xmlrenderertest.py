"""XMLRenderer: structure, escaping, unusual keys, negotiation."""

import json
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID

import pytest

from hinoki import Hinoki, JSONRenderer, Request, XMLRenderer, http
from hinoki.renderers import xml_text
from hinoki.schema import encode
from tests.harness import Pool

DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>'


def render(value: object, renderer: XMLRenderer | None = None) -> str:
    request: Any = None
    return (renderer or XMLRenderer()).render(value, request).decode()


def test_mappings_lists_and_scalars() -> None:
    value = {
        "id": 7,
        "ok": True,
        "off": False,
        "ratio": 1.5,
        "none": None,
        "tags": ["a", "b"],
    }
    assert render(value) == (
        f"{DECLARATION}<response><id>7</id><ok>true</ok><off>false</off>"
        "<ratio>1.5</ratio><none/><tags><item>a</item><item>b</item></tags></response>"
    )


def test_root_and_item_names_are_configurable() -> None:
    renderer = XMLRenderer(root="orders", item="order")
    assert render([1, {"a": 2}], renderer) == (
        f"{DECLARATION}<orders><order>1</order><order><a>2</a></order></orders>"
    )


def test_text_is_escaped_and_illegal_characters_are_replaced() -> None:
    assert render({"v": '<a & "b">\x00\ud800'}) == (
        f"{DECLARATION}<response><v>&lt;a &amp; &quot;b&quot;&gt;"
        "\ufffd\ufffd</v></response>"
    )


@pytest.mark.parametrize("key", ["with space", "1st", "xmlns", "a>b", "", 'q"'])
def test_a_key_that_is_not_a_name_becomes_an_item(key: str) -> None:
    assert render({key: 1}).startswith(f"{DECLARATION}<response><item key=")
    assert "</item></response>" in render({key: 1})


def test_a_hostile_key_cannot_break_out_of_the_attribute() -> None:
    body = render({'x" onload="1': 1})
    assert body.endswith('<item key="x&quot; onload=&quot;1">1</item></response>')


def test_whitespace_survives_and_special_floats_use_schema_literals() -> None:
    body = render(
        {"a\nb": "x\r\ny\t", "f": [float("nan"), float("inf"), -float("inf")]}
    )
    assert 'key="a&#10;b">x&#13;&#10;y&#9;<' in body
    assert "<item>NaN</item><item>INF</item><item>-INF</item>" in body
    with pytest.raises(ValueError, match="too large"):
        render({"n": 10**5000})


def test_unknown_types_nesting_and_names_are_refused() -> None:
    with pytest.raises(TypeError):
        render({"v": object()})
    deep: object = 1
    for _ in range(250):
        deep = [deep]
    with pytest.raises(ValueError, match="nested"):
        render(deep)
    for bad in ("", "1a", "has space", "xml"):
        with pytest.raises(ValueError, match="element names"):
            XMLRenderer(root=bad)


@http.get("/report", renderers=[JSONRenderer(), XMLRenderer(root="report")])
def report(request: Request) -> dict[str, object]:
    return {"id": 1, "items": ["x"]}


async def test_xml_is_negotiated(pool: Pool) -> None:
    app = Hinoki()
    app.include(report)
    client = await pool.start(app)
    xml = await client.request("GET", "/report", headers={"accept": "application/xml"})
    assert xml.status == 200
    assert xml.headers["content-type"] == "application/xml; charset=utf-8"
    assert xml.body == (
        f"{DECLARATION}<report><id>1</id><items><item>x</item></items></report>"
    )
    json_reply = await client.request("GET", "/report")
    assert json_reply.headers["content-type"].startswith("application/json")
    refused = await client.request("GET", "/report", headers={"accept": "text/csv"})
    assert refused.status == 406


STAMP = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@http.get("/held", renderers=[JSONRenderer(), XMLRenderer()])
async def held() -> dict[str, object]:
    return {"ids": (UUID(int=1),), "at": (STAMP, [(STAMP,)])}


async def test_a_tuple_of_uuids_and_datetimes_renders_like_json(pool: Pool) -> None:
    app = Hinoki()
    app.include(held)
    client = await pool.start(app)
    as_json = await client.request("GET", "/held")
    as_xml = await client.request("GET", "/held", headers={"accept": "application/xml"})
    assert (as_json.status, as_xml.status) == (200, 200)
    uid, stamp = str(UUID(int=1)), STAMP.isoformat()
    assert as_json.body == {"ids": [uid], "at": [stamp, [[stamp]]]}
    assert as_xml.body == (
        f"{DECLARATION}<response><ids><item>{uid}</item></ids><at><item>{stamp}"
        f"</item><item><item><item>{stamp}</item></item></item></at></response>"
    )


def test_encode_turns_tuples_into_lists() -> None:
    assert encode((UUID(int=1), (STAMP,))) == [str(UUID(int=1)), [STAMP.isoformat()]]


class Shade(str, Enum):  # noqa: UP042 - its str() is not its value
    DARK = "dark"


def test_none_bool_and_int_keys_are_spelled_like_json() -> None:
    value = {None: 1, True: 2, False: 3, 7: 4, "k": 5, Shade.DARK: 6}
    assert json.loads(JSONRenderer().render(value, None)) == {  # type: ignore[arg-type]
        "null": 1,
        "true": 2,
        "false": 3,
        "7": 4,
        "k": 5,
        "dark": 6,
    }
    assert render(value) == (
        f"{DECLARATION}<response><null>1</null><true>2</true><false>3</false>"
        '<item key="7">4</item><k>5</k><dark>6</dark></response>'
    )


@pytest.mark.parametrize("key", [1.5, UUID(int=1), STAMP, (1, 2), b"x"])
def test_keys_json_refuses_are_refused(key: object) -> None:
    with pytest.raises(TypeError, match="keys must be str, int, bool or None"):
        JSONRenderer().render({key: 1}, None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="keys must be str, int, bool or None"):
        render({key: 1})


def legal(char: str) -> bool:
    return (
        char in "\t\n\r"
        or "\u0020" <= char <= "\ud7ff"
        or "\ue000" <= char <= "\ufffd"
        or char >= "\U00010000"
    )


def reference_text(value: str) -> str:
    """Character-by-character escaping: what ``xml_text`` must match."""
    escapes = {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "\t": "&#9;",
        "\n": "&#10;",
        "\r": "&#13;",
    }
    return "".join(escapes.get(c, c) if legal(c) else "\ufffd" for c in value)


TRICKY = [
    "",
    "plain",
    "]]>",
    "a]]>b]]]>",
    "<&>\"'",
    "&amp;",
    "\x00\x01\x08\x0b\x0c\x1f\x7f\x85",
    "\t\n\r\r\n",
    "\ud800\udfff\ud83d",
    "\ufffe\uffff\ufffd",
    "\U0001f600\U0010ffff",
    "\ud7ff\ue000",
    "caf\u00e9 \u65e5\u672c",
    "<" * 100 + "&" * 100,
]


@pytest.mark.parametrize("text", TRICKY)
def test_escaping_matches_the_character_rules(text: str) -> None:
    assert xml_text(text) == reference_text(text)


def test_text_without_special_characters_is_returned_as_is() -> None:
    text = "nothing to escape here " * 10
    assert xml_text(text) is text
