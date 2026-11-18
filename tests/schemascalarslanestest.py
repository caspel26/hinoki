"""``date``, ``time``, ``timedelta``, ``Decimal`` and ``bytes`` fields through
every request source and both request lanes: JSON bodies, forms, query strings
and XML, on the ASGI lane and on the Rust server's native lane."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from hinoki import XML, Field, Form, Hinoki, Query, Schema, XMLParser, http
from hinoki.runtime.rustserver import RustWorker
from hinoki.testing import ASGIClient
from tests.harness import Pool
from tests.rustservertest import JSON, fetch
from tests.xmlnativelanetest import recording

FORM = "application/x-www-form-urlencoded"


class Slot(Schema):
    day: dt.date
    at: dt.time
    wait: dt.timedelta = dt.timedelta(0)
    fee: Decimal = Decimal(0)
    token: bytes = b""
    days: list[dt.date] = Field(default_factory=list)


def shown(slot: Slot) -> dict[str, Any]:
    return {
        "day": slot.day.isoformat(),
        "at": slot.at.isoformat(),
        "wait": slot.wait.total_seconds(),
        "fee": str(slot.fee),
        "token": slot.token.hex(),
        "days": [day.isoformat() for day in slot.days],
    }


@http.post("/lanes/json")
async def json_slot(data: Slot) -> Slot:
    return data


@http.post("/lanes/form")
def form_slot(data: Form[Slot]) -> dict[str, Any]:
    return shown(data)


@http.get("/lanes/query")
def query_slot(filters: Query[Slot]) -> dict[str, Any]:
    return shown(filters)


@http.post("/lanes/xml", parsers=[XMLParser()])
def xml_slot(data: XML[Slot]) -> dict[str, Any]:
    return shown(data)


EXPECTED = {
    "day": "2026-11-18",
    "at": "09:30:00+01:00",
    "wait": 90.5,
    "fee": "12.50",
    "token": "fbff",
    "days": ["2026-01-01", "2026-01-02"],
}


async def test_forms_read_the_text_forms(pool: Pool) -> None:
    client = await pool.start(Hinoki(include=[form_slot]))
    reply = await client.request(
        "POST",
        "/lanes/form",
        b"day=2026-11-18&at=09%3A30%3A00%2B01%3A00&wait=PT1M30.5S&fee=12.50"
        b"&token=%2B%2F8%3D&days=2026-01-01&days=2026-01-02",
        content_type=FORM,
    )
    assert reply.status == 200, reply.body
    assert reply.body == EXPECTED
    reply = await client.request(
        "POST",
        "/lanes/form",
        b"day=2026-11-18&at=09:30:00&wait=90&fee=1e3&token=+/8=&days=2026-1-1",
        content_type=FORM,
    )
    assert reply.status == 422
    assert [(d["path"], d["code"]) for d in reply.body["details"]] == [
        ("data.at", "time_tz"),
        ("data.wait", "duration"),
        ("data.fee", "decimal"),
        ("data.token", "bytes"),
        ("data.days[0]", "date"),
    ]


async def test_query_strings_read_the_text_forms(pool: Pool) -> None:
    client = await pool.start(Hinoki(include=[query_slot]))
    reply = await client.request(
        "GET",
        "/lanes/query",
        query="day=2026-11-18&at=09:30:00%2B01:00&wait=PT1M30.5S&fee=12.50"
        "&token=%2B/8%3D&days=2026-01-01&days=2026-01-02",
    )
    assert reply.status == 200, reply.body
    assert reply.body == EXPECTED
    reply = await client.request(
        "GET", "/lanes/query", query="day=18-11-2026&at=09:30:00Z&fee=1.5.0"
    )
    assert [(d["path"], d["code"]) for d in reply.body["details"]] == [
        ("filters.day", "date"),
        ("filters.fee", "decimal"),
    ]


async def test_xml_reads_the_text_forms(pool: Pool) -> None:
    client = await pool.start(Hinoki(include=[xml_slot]))
    reply = await client.request(
        "POST",
        "/lanes/xml",
        b"<slot><day>2026-11-18</day><at>09:30:00+01:00</at><wait>PT1M30.5S</wait>"
        b"<fee>12.50</fee><token>+/8=</token><days>2026-01-01</days>"
        b"<days>2026-01-02</days></slot>",
        content_type="application/xml",
    )
    assert reply.status == 200, reply.body
    assert reply.body == EXPECTED
    reply = await client.request(
        "POST",
        "/lanes/xml",
        b"<slot><day>2026-11-18</day><at>09:30:00Z</at><token>+/8</token></slot>",
        content_type="application/xml",
    )
    assert [(d["path"], d["code"]) for d in reply.body["details"]] == [
        ("data.token", "bytes")
    ]


BODIES = [
    b'{"day": "2026-11-18", "at": "09:30:00+01:00", "wait": "PT1M30.5S",'
    b' "fee": "12.50", "token": "+/8=", "days": ["2026-01-01"]}',
    b'{"day": "2026-11-18", "at": "09:30:00Z", "fee": 7, "wait": "-P1D"}',
    b'{"day": "2026-02-30", "at": "09:30:00", "wait": 5, "fee": 1.5,'
    b' "token": "+/8", "days": ["x"]}',
    b'{"day": "2026-11-18", "at": "09:30:00Z", "fee": "' + b"9" * 65 + b'"}',
    b'{"day": "2026-11-18", "at": "09:30:00Z", "fee": "NaN", "wait": "P1000000000D"}',
]


async def test_the_native_lane_validates_and_encodes_like_the_python_lane() -> None:
    app = Hinoki()
    app.include(json_slot, query_slot)
    worker = RustWorker(app, "127.0.0.1", 0)
    lanes: list[str] = []
    pipeline = worker.pipeline
    pipeline.handle_native = recording(lanes, "native", pipeline.handle_native)  # type: ignore[method-assign]
    pipeline.handle = recording(lanes, "python", pipeline.handle)  # type: ignore[method-assign]
    query = "/lanes/query?day=2026-11-18&at=09:30:00Z&token=AA%3D%3D&fee=0.10"
    port = await worker.start()
    try:
        native = [
            await fetch(
                port,
                "POST",
                "/lanes/json",
                JSON + f"content-length: {len(body)}\r\n",
                body=body,
            )
            for body in BODIES
        ]
        queried = await fetch(port, "GET", query)
    finally:
        await worker.stop()
    assert lanes == ["native"] * (len(BODIES) + 1)
    async with ASGIClient(app) as client:
        for body, got in zip(BODIES, native, strict=True):
            expected = await client.post(
                "/lanes/json", body=body, headers={"content-type": "application/json"}
            )
            assert got.status == expected.status
            if got.status == 200:
                assert got.body == expected.raw
            else:
                assert got.json()["details"] == expected.body["details"]
        path, _, raw_query = query.partition("?")
        expected = await client.get(path, query=raw_query)
        assert queried.status == expected.status == 200
        assert queried.json() == expected.body
    assert native[0].body == (
        b'{"day":"2026-11-18","at":"09:30:00+01:00","wait":"PT1M30.5S",'
        b'"fee":"12.50","token":"+/8=","days":["2026-01-01"]}'
    )
    assert native[1].json()["wait"] == "-P1D"
    assert native[1].json()["fee"] == "7"
    assert [d["code"] for d in native[2].json()["details"]] == [
        "date",
        "time_tz",
        "duration",
        "decimal",
        "bytes",
        "date",
    ]
    assert [d["code"] for d in native[3].json()["details"]] == ["max_digits"]
    assert [d["code"] for d in native[4].json()["details"]] == [
        "duration_range",
        "decimal",
    ]
