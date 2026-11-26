"""``send_json`` with ``reply=`` sends the declared fields of a subclass instance."""

from __future__ import annotations

import json

from hinoki import Schema
from hinoki.websocket import WebSocket

SECRET = "s3cret-socket"  # noqa: S105 - a marker, not a credential


class Public(Schema):
    id: int


class Private(Public):
    secret: str


class Quiet(Public):
    """Decides its own ``dump()``: the declared class keeps it."""

    def dump(self, **_: object) -> dict[str, object]:
        return {"id": self.id, "quiet": True}


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = None

    async def send_text(self, text: str) -> bool:
        self.sent.append(text)
        return True


def socket(reply: type[Schema] | None) -> tuple[WebSocket, FakeTransport]:
    transport = FakeTransport()
    return WebSocket(transport, path="/w", reply=reply), transport  # type: ignore[arg-type]


async def test_a_subclass_of_the_reply_is_sent_as_the_reply() -> None:
    ws, transport = socket(Public)
    await ws.send_json(Private(id=1, secret=SECRET))
    assert [json.loads(text) for text in transport.sent] == [{"id": 1}]
    assert SECRET not in transport.sent[0]


async def test_the_reply_itself_and_a_dict_are_sent_as_before() -> None:
    ws, transport = socket(Public)
    await ws.send_json(Public(id=2))
    await ws.send_json({"id": 3})
    assert [json.loads(text) for text in transport.sent] == [{"id": 2}, {"id": 3}]


async def test_a_reply_that_decides_its_own_dump_keeps_it() -> None:
    ws, transport = socket(Quiet)
    await ws.send_json(Quiet(id=1))
    assert json.loads(transport.sent[0]) == {"id": 1, "quiet": True}


async def test_without_a_reply_the_value_is_sent_whole() -> None:
    ws, transport = socket(None)
    await ws.send_json(Private(id=1, secret=SECRET))
    assert json.loads(transport.sent[0]) == {"id": 1, "secret": SECRET}
