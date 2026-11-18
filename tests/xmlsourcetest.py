"""``XMLParser`` and ``XML[Schema]``."""

import dataclasses
from typing import Any

import pytest

from hinoki import XML, FormParser, Hinoki, Request, Schema, XMLParser, http
from hinoki.errors import RegistrationError
from tests.harness import Client, Pool

XML_TYPE = "application/xml"


class Line(Schema):
    sku: str
    qty: int


class Order(Schema):
    customer: str
    paid: bool = False
    lines: list[Line] = dataclasses.field(default_factory=list)
    tags: list[int] = dataclasses.field(default_factory=list)


@http.post("/tree", parsers=[XMLParser()])
def tree(request: Request) -> dict[str, Any]:
    return {"data": request.data}


@http.post("/order", status=201)
def order(data: XML[Order]) -> dict[str, Any]:
    return {"customer": data.customer, "paid": data.paid, "n": len(data.lines)}


def send(client: Client, body: bytes, path: str = "/tree") -> Any:
    return client.request("POST", path, body, content_type=XML_TYPE)


async def test_elements_map_to_dicts_lists_text_and_none(pool: Pool) -> None:
    client = await pool.start(Hinoki(include=[tree]))
    reply = await send(
        client,
        b"<?xml version='1.0'?><r><a>1</a><b><c>x</c></b><i>1</i><i>2</i>"
        b"<e/><f></f><g>&lt;&#65;&amp;<![CDATA[<z>]]></g></r>",
    )
    assert reply.body["data"] == {
        "a": "1",
        "b": {"c": "x"},
        "i": ["1", "2"],
        "e": None,
        "f": None,
        "g": "<A&<z>",
    }


@pytest.mark.parametrize(
    "body",
    [
        b"<!DOCTYPE r [<!ENTITY x 'y'>]><r>&x;</r>",
        b"<r>&custom;</r>",
        b"<r><a></r>",
        b"<r>x<a/></r>",
        b"<r/><r/>",
        b"text",
        b"",
        b"<r>\xff</r>",
    ],
)
async def test_unsafe_or_malformed_xml_is_a_400(pool: Pool, body: bytes) -> None:
    client = await pool.start(Hinoki(include=[tree]))
    reply = await send(client, body)
    assert (reply.status, reply.body["error"]) == (400, "invalid_xml")


async def test_depth_and_element_limits(pool: Pool) -> None:
    @http.post("/small", parsers=[XMLParser(max_depth=3, max_elements=4)])
    def small(request: Request) -> dict[str, Any]:
        return {"ok": request.data is not None}

    client = await pool.start(Hinoki(include=[small]))
    assert (await send(client, b"<a><b><c/></b></a>", "/small")).status == 200
    assert (await send(client, b"<a><b><c><d/></c></b></a>", "/small")).status == 400
    assert (await send(client, b"<a><b/><b/><b/><b/></a>", "/small")).status == 400


async def test_xml_schema_converts_text_then_validates(pool: Pool) -> None:
    client = await pool.start(Hinoki(include=[order]))
    reply = await send(
        client,
        b"<order><customer>Ada</customer><paid>true</paid>"
        b"<lines><sku>a</sku><qty>2</qty></lines>"
        b"<lines><sku>b</sku><qty>3</qty></lines><tags>7</tags></order>",
        "/order",
    )
    assert (reply.status, reply.body) == (
        201,
        {"customer": "Ada", "paid": True, "n": 2},
    )
    bad = await send(
        client, b"<order><customer>Ada</customer><paid>maybe</paid></order>", "/order"
    )
    assert bad.status == 422


def test_xml_registration_rules() -> None:
    @http.post("/m", parsers=[XMLParser(), FormParser()])
    def mixed(data: XML[Order]) -> None: ...

    with pytest.raises(RegistrationError, match="only XMLParser"):
        Hinoki(include=[mixed])


def test_xml_openapi_and_limits() -> None:
    content = Hinoki(include=[order]).openapi()["paths"]["/order"]["post"][
        "requestBody"
    ]["content"]
    assert set(content) == {XML_TYPE}
    with pytest.raises(ValueError, match="positive"):
        XMLParser(max_depth=0)
