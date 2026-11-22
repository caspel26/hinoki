"""XMLRenderer: structure, escaping, unusual keys, negotiation."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

import pytest

from hinoki import Hinoki, JSONRenderer, Request, Schema, XMLRenderer, core, http
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


class Price(Schema):
    amount: Decimal
    day: date
    tags: set[str]
    shade: Shade
    note: str | None = None


class Dumped:
    def dump(self) -> dict[str, object]:
        return {"a": [1, None], "b": UUID(int=2)}


def test_a_schema_instance_has_wire_forms_inside_and_a_dump_object_works() -> None:
    price = Price(
        amount=Decimal("1.50"), day=date(2026, 1, 2), tags={"b", "a"}, shade=Shade.DARK
    )
    assert render({"p": price, "d": Dumped()}) == (
        f"{DECLARATION}<response><p><amount>1.50</amount><day>2026-01-02</day>"
        "<tags><item>a</item><item>b</item></tags><shade>dark</shade><note/></p>"
        f"<d><a><item>1</item><item/></a><b>{UUID(int=2)}</b></d></response>"
    )
    assert render(price) == render(json.loads(JSONRenderer().render(price, None)))  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [Decimal(1), date(2026, 1, 2), {1, 2}, b"x"])
def test_values_json_refuses_outside_a_schema_are_refused(value: object) -> None:
    with pytest.raises(TypeError, match="cannot be rendered as XML"):
        render({"v": value})
    with pytest.raises(TypeError, match="not JSON serializable"):
        JSONRenderer().render({"v": value}, None)  # type: ignore[arg-type]


def test_floats_are_written_as_json_writes_them() -> None:
    values = [0.1, -0.0, 1e16, 1e-7, 123456.789]
    written = JSONRenderer().render(values, None).decode()  # type: ignore[arg-type]
    expected = "".join(f"<item>{text}</item>" for text in written[1:-1].split(","))
    assert render(values) == f"{DECLARATION}<response>{expected}</response>"


def test_the_nesting_limit_is_the_json_one() -> None:
    deep: object = 1
    for _ in range(200):
        deep = [deep]
    assert render(deep).count("<item>") == 200
    with pytest.raises(ValueError, match="nested too deeply"):
        render([deep])


def test_a_dump_that_never_settles_is_bounded() -> None:
    class Forever:
        def dump(self) -> object:
            return Forever()

    with pytest.raises(ValueError, match="nested too deeply"):
        render(Forever())


def test_root_and_item_are_checked_by_the_encoder_too() -> None:
    assert core.xml_name_ok("a.b-c") and not core.xml_name_ok("xml")
    with pytest.raises(ValueError, match="element names"):
        core.encode_xml(1, "bad name")
    with pytest.raises(ValueError, match="element names"):
        core.encode_xml(1, "ok", "1")
    assert core.encode_xml(1) == f"{DECLARATION}<response>1</response>".encode()


def test_what_a_round_trip_keeps_is_what_the_docs_say() -> None:
    def back(value: object) -> object:
        return core.parse_xml_body(core.encode_xml(value), 32, 10_000)

    empties: list[object] = [None, "", {}, []]
    assert [back(v) for v in empties] == [None] * 4
    assert back({"a": 1, "b": 1.5, "c": True}) == {"a": "1", "b": "1.5", "c": "true"}
    assert back([1]) == {"item": "1"}
    assert back([1, 2]) == {"item": ["1", "2"]}
    assert back({"a b": 1}) == {"item": "1"}
    assert back({"a": "x\ud800\x00"}) == {"a": "x\ufffd\ufffd"}


def legal(char: str) -> bool:
    return (
        char in "\t\n\r"
        or "\u0020" <= char <= "\ud7ff"
        or "\ue000" <= char <= "\ufffd"
        or char >= "\U00010000"
    )


def reference_text(value: str) -> str:
    """Character-by-character escaping: what the encoder must match."""
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
    assert render({"v": text}) == (
        f"{DECLARATION}<response><v>{reference_text(text)}</v></response>"
    )
    assert render([text]) == (
        f"{DECLARATION}<response><item>{reference_text(text)}</item></response>"
    )


@pytest.mark.parametrize("text", TRICKY)
def test_a_key_written_as_an_attribute_is_escaped_like_text(text: str) -> None:
    key = f"{text} "
    assert render({key: 1}) == (
        f'{DECLARATION}<response><item key="{reference_text(key)}">1</item></response>'
    )
