"""XMLRenderer: structure, escaping, unusual keys, negotiation."""

from typing import Any

import pytest

from hinoki import Hinoki, JSONRenderer, Request, XMLRenderer, http
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
