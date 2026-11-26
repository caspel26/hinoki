"""A ``@computed_field`` whose return annotation names a Schema sends that Schema's
fields, whatever subclass instance the method returns, in every encoder."""

from __future__ import annotations

import json
from typing import Any

import pytest

from hinoki import Hinoki, Schema, XMLRenderer, computed_field, core
from hinoki.routing import RouteGroup
from hinoki.runtime.kiso import KisoWorker
from hinoki.testing import ASGIClient
from tests.kisoservertest import fetch

SECRET = "s3cret-computed"  # noqa: S105 - a marker, not a credential


class Public(Schema):
    id: int


class Private(Public):
    secret: str


class Page(Schema):
    id: int

    @computed_field
    def child(self) -> Public:
        return Private(id=self.id + 1, secret=SECRET)

    @computed_field
    def maybe(self) -> Public | None:
        return Private(id=self.id + 2, secret=SECRET)

    @computed_field
    def children(self) -> list[Public]:
        return [Private(id=self.id + 3, secret=SECRET)]

    @computed_field
    def by_name(self) -> dict[str, Public]:
        return {"a": Private(id=self.id + 4, secret=SECRET)}

    @computed_field
    def later(self) -> Late:
        return LatePrivate(n=1, secret=SECRET)


class Late(Schema):
    n: int


class LatePrivate(Late):
    secret: str


class Wrapper(Schema):
    page: Page


class Exact(Schema):
    @computed_field
    def child(self) -> Public:
        return Public(id=7)


EXPECTED_PAGE = {
    "id": 1,
    "child": {"id": 2},
    "maybe": {"id": 3},
    "children": [{"id": 4}],
    "by_name": {"a": {"id": 5}},
    "later": {"n": 1},
}


def test_dump_and_the_json_encoder_agree() -> None:
    page = Page(id=1)
    assert page.dump() == EXPECTED_PAGE
    written = core.encode_json(page, None, False, None)
    assert json.loads(written) == EXPECTED_PAGE
    assert SECRET.encode() not in written


def test_a_computed_field_inside_a_field_is_narrowed() -> None:
    written = core.encode_json(Wrapper(page=Page(id=1)), None, False, None)
    assert json.loads(written) == {"page": EXPECTED_PAGE}


def test_exact_values_are_written_unchanged() -> None:
    assert core.encode_json(Exact(), None, False, None) == b'{"child":{"id":7}}'
    assert Exact().dump() == {"child": {"id": 7}}


def handler(annotation: Any, value: Any) -> Any:
    async def serve() -> Any:
        return value

    serve.__annotations__ = {"return": annotation}
    serve.__name__ = "serve"
    return serve


def build(*, xml: bool = False) -> Hinoki:
    group = RouteGroup("")
    options: dict[str, Any] = {"renderers": [XMLRenderer(root="r")]} if xml else {}
    group.get("/page", **options)(handler(Page, Page(id=1)))
    group.get("/wrapper", **options)(handler(Wrapper, Wrapper(page=Page(id=1))))
    group.get("/list", **options)(handler(list[Page], [Page(id=1)]))
    return Hinoki(include=[group])


EXPECTED = {
    "/page": EXPECTED_PAGE,
    "/wrapper": {"page": EXPECTED_PAGE},
    "/list": [EXPECTED_PAGE],
}


async def test_asgi_sends_the_declared_fields() -> None:
    async with ASGIClient(build()) as client:
        for path, expected in EXPECTED.items():
            reply = await client.get(path)
            assert reply.status == 200, path
            assert SECRET.encode() not in reply.raw, path
            assert json.loads(reply.raw) == expected, path


@pytest.mark.parametrize("native", [True, False])
async def test_kiso_sends_the_declared_fields(native: bool) -> None:
    worker = KisoWorker(build(), "127.0.0.1", 0, native_dispatch=native)
    port = await worker.start()
    try:
        for path, expected in EXPECTED.items():
            reply = await fetch(port, "GET", path)
            assert reply.status == 200, path
            assert SECRET.encode() not in reply.body, path
            assert json.loads(reply.body) == expected, path
    finally:
        await worker.stop()


async def test_the_xml_renderer_sends_the_declared_fields() -> None:
    async with ASGIClient(build(xml=True)) as client:
        for path in EXPECTED:
            reply = await client.get(path)
            assert reply.status == 200, path
            assert SECRET not in reply.raw.decode(), path
