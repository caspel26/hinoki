"""Every annotation shape that holds a Schema sends the declared fields only: an
abstract collection, a PEP 695 alias, a container of another kind than declared
and a recursive TypedDict, on the ASGI pipeline, both Kiso lanes and the XML
renderer. A value that cannot be walked is documented as written whole."""

from __future__ import annotations

import collections.abc
import json
import typing
from typing import Any, TypedDict

import pytest

from hinoki import Hinoki, Schema, XMLRenderer
from hinoki.errors import RegistrationError
from hinoki.routing import RouteGroup
from hinoki.runtime.kiso import KisoWorker
from hinoki.testing import ASGIClient
from tests.kisoservertest import fetch

SECRET = "s3cret-shapes"  # noqa: S105 - a marker, not a credential


class Public(Schema):
    id: int


class Private(Public):
    secret: str


def private(n: int = 1) -> Private:
    return Private(id=n, secret=SECRET)


def whole(n: int) -> dict[str, Any]:
    """What a position without a guide of its own sends."""
    return {"id": n, "secret": SECRET}


def shown(n: int = 1) -> dict[str, Any]:
    return {"id": n}


type Alias = list[Public]
type NestedAlias = dict[str, Alias]
type Box[T] = list[T]
type Loop = list[Loop] | Public


class Tree(TypedDict):
    item: Public
    child: Tree | None


class Forest(TypedDict):
    kids: list[Forest]
    item: Public


Cases = dict[str, tuple[Any, Any, object]]
CASES: Cases = {
    # path: (return annotation, value, what is sent)
    "/sequence": (collections.abc.Sequence[Public], [private(1)], [shown(1)]),
    "/typing-sequence": (typing.Sequence[Public], [private(1)], [shown(1)]),
    "/iterable": (collections.abc.Iterable[Public], [private(1)], [shown(1)]),
    "/collection": (collections.abc.Collection[Public], [private(1)], [shown(1)]),
    "/mapping": (
        collections.abc.Mapping[str, Public],
        {"a": private(1)},
        {"a": shown(1)},
    ),
    "/typing-mapping": (
        typing.Mapping[str, Public],
        {"a": private(1)},
        {"a": shown(1)},
    ),
    "/alias": (Alias, [private(1)], [shown(1)]),
    "/nested-alias": (NestedAlias, {"a": [private(1)]}, {"a": [shown(1)]}),
    "/generic-alias": (Box[Public], [private(1)], [shown(1)]),
    "/optional-alias": (Alias | None, [private(1)], [shown(1)]),
    "/tuple-as-list": (list[Public], (private(1), private(2)), [shown(1), shown(2)]),
}
# A recursive TypedDict is narrowed at its first level: the back-reference to
# itself is written as it is.
RECURSIVE: Cases = {
    "/recursive": (
        Tree,
        {"item": private(1), "child": {"item": private(2), "child": None}},
        {
            "item": shown(1),
            "child": {"item": {"id": 2, "secret": SECRET}, "child": None},
        },
    ),
    "/recursive-list": (
        Forest,
        {"kids": [{"kids": [], "item": private(2)}], "item": private(1)},
        {"kids": [{"kids": [], "item": whole(2)}], "item": shown(1)},
    ),
}


def handler(annotation: Any, value: Any) -> Any:
    async def serve() -> Any:
        return value

    serve.__annotations__ = {"return": annotation}
    serve.__name__ = "serve"
    return serve


def build(
    cases: dict[str, tuple[Any, Any, object]] = CASES, *, xml: bool = False
) -> Hinoki:
    group = RouteGroup("")
    options: dict[str, Any] = {"renderers": [XMLRenderer(root="r")]} if xml else {}
    for path, (annotation, value, _) in cases.items():
        group.get(path, validate_response=False, **options)(handler(annotation, value))
    return Hinoki(include=[group])


def check(path: str, body: bytes) -> None:
    assert SECRET.encode() not in body, path
    assert json.loads(body) == CASES[path][2], path


@pytest.mark.parametrize("path", sorted(CASES))
async def test_asgi_sends_the_declared_fields(path: str) -> None:
    async with ASGIClient(build()) as client:
        reply = await client.get(path)
    assert reply.status == 200
    check(path, reply.raw)


@pytest.mark.parametrize("native", [True, False])
async def test_kiso_sends_the_declared_fields(native: bool) -> None:
    worker = KisoWorker(build(), "127.0.0.1", 0, native_dispatch=native)
    port = await worker.start()
    try:
        for path in CASES:
            reply = await fetch(port, "GET", path)
            assert reply.status == 200, path
            check(path, reply.body)
    finally:
        await worker.stop()


@pytest.mark.parametrize("path", sorted(CASES))
async def test_the_xml_renderer_sends_the_declared_fields(path: str) -> None:
    async with ASGIClient(build(xml=True)) as client:
        reply = await client.get(path)
    assert reply.status == 200
    assert SECRET not in reply.raw.decode()
    assert b"<id>" in reply.raw


@pytest.mark.parametrize("path", sorted(RECURSIVE))
async def test_a_recursive_typeddict_builds_and_narrows_its_first_level(
    path: str,
) -> None:
    async with ASGIClient(build(RECURSIVE)) as client:
        reply = await client.get(path)
    assert reply.status == 200
    assert json.loads(reply.raw) == RECURSIVE[path][2]


@pytest.mark.parametrize("native", [True, False])
async def test_a_recursive_typeddict_over_kiso(native: bool) -> None:
    worker = KisoWorker(build(RECURSIVE), "127.0.0.1", 0, native_dispatch=native)
    port = await worker.start()
    try:
        for path, (_, _, expected) in RECURSIVE.items():
            reply = await fetch(port, "GET", path)
            assert reply.status == 200, path
            assert json.loads(reply.body) == expected, path
    finally:
        await worker.stop()


async def test_a_type_alias_is_checked_like_what_it_names() -> None:
    group = RouteGroup("")
    group.get("/ok")(handler(Alias, [private(1)]))
    group.get("/bad")(handler(Alias, "text"))
    async with ASGIClient(Hinoki(include=[group])) as client:
        ok = await client.get("/ok")
        bad = await client.get("/bad")
    assert ok.status == 200
    assert json.loads(ok.raw) == [shown(1)]
    assert SECRET.encode() not in ok.raw
    assert bad.status == 500


def test_a_recursive_type_alias_is_refused_when_checked() -> None:
    group = RouteGroup("")
    group.get("/loop")(handler(Loop, []))
    with pytest.raises(RegistrationError, match="recursive"):
        Hinoki(include=[group])
    group = RouteGroup("")
    group.get("/loop", validate_response=False)(handler(Loop, [private(1)]))
    with pytest.raises(RegistrationError, match="recursive"):
        Hinoki(include=[group])
