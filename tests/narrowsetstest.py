"""``set[S]``, ``frozenset[S]`` and ``collections.abc.Set[S]`` results are narrowed
member by member and written as a list, in every encoder."""

from __future__ import annotations

import collections.abc
import json
from typing import Any

import pytest

from hinoki import Hinoki, Schema, XMLRenderer
from hinoki.routing import RouteGroup
from hinoki.testing import ASGIClient

SECRET = "s3cret-set"  # noqa: S105 - a marker, not a credential


class Public(Schema):
    id: int


class Private(Public):
    secret: str


def private(n: int = 1) -> Private:
    return Private(id=n, secret=SECRET)


def handler(annotation: Any, value: Any) -> Any:
    async def serve() -> Any:
        return value

    serve.__annotations__ = {"return": annotation}
    return serve


SHAPES: dict[str, tuple[Any, Any]] = {
    "set": (set[Public], {private()}),
    "frozenset": (frozenset[Public], frozenset({private()})),
    "abc-set": (collections.abc.Set[Public], frozenset({private()})),
    "mutable-set": (collections.abc.MutableSet[Public], {private()}),
    "dict-of-set": (dict[str, set[Public]], {"a": {private()}}),
    "optional-set": (set[Public] | None, {private()}),
}


@pytest.mark.parametrize("xml", [False, True])
@pytest.mark.parametrize("name", sorted(SHAPES))
async def test_a_set_of_subclass_instances_is_narrowed(name: str, xml: bool) -> None:
    annotation, value = SHAPES[name]
    group = RouteGroup("")
    options: dict[str, Any] = {"renderers": [XMLRenderer(root="r")]} if xml else {}
    group.get("/x", validate_response=False, **options)(handler(annotation, value))
    async with ASGIClient(Hinoki(include=[group])) as client:
        reply = await client.get("/x")
    assert reply.status == 200
    assert SECRET.encode() not in reply.raw
    if not xml:
        assert b'"id":1' in reply.raw.replace(b" ", b"")


async def test_a_set_of_exact_members_is_unchanged() -> None:
    group = RouteGroup("")
    group.get("/x", validate_response=False)(handler(set[Public], {Public(id=1)}))
    async with ASGIClient(Hinoki(include=[group])) as client:
        reply = await client.get("/x")
    assert json.loads(reply.raw) == [{"id": 1}]
