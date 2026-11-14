"""Native serialisation must equal the Python ``encode`` it replaces."""

import json
import random
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from hinoki import UNSET, Schema, Unset, core
from hinoki.pagination import Page
from hinoki.schema import encode
from tests.schemadifftest import Node, Post, Tag, valid_post


class Public(Schema):
    name: str
    nick: str | Unset = UNSET


class Private(Public):
    password_hash: str


def same(value: object) -> None:
    assert json.loads(core.encode_json(value)) == encode(value)


@pytest.mark.parametrize("seed", range(6))
def test_posts_serialise_like_the_python_walk(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(200):
        same(Post.validate(valid_post(rng)))


def test_unset_is_omitted_and_none_is_kept() -> None:
    assert core.encode_json(Public(name="a")) == b'{"name":"a"}'
    assert core.encode_json(Public(name="a", nick="n")) == b'{"name":"a","nick":"n"}'
    same(Public(name="a"))


def test_a_subclass_uses_its_own_fields_not_its_parents() -> None:
    Public.native_schema()  # the parent is compiled first
    wide = Private(name="a", password_hash="h")
    assert json.loads(core.encode_json(wide)) == {"name": "a", "password_hash": "h"}
    same(wide)


def test_uuid_datetime_nesting_and_pages() -> None:
    key = uuid4()
    moment = datetime(2026, 11, 13, 9, 30, tzinfo=timezone(timedelta(hours=2)))
    tag = Tag(label="x", weight=2)
    assert json.loads(core.encode_json({"id": key, "at": moment, "tags": [tag]})) == {
        "id": str(key),
        "at": moment.isoformat(),
        "tags": [{"label": "x", "weight": 2}],
    }
    page = Page([tag, tag], 1, 10, 2)
    same(page)
    tree = Node(name="r", children=[Node(name="c")])
    same(tree)
    assert (
        core.encode_json(datetime(2026, 1, 1, tzinfo=UTC))
        == b'"2026-01-01T00:00:00+00:00"'
    )


def test_dump_is_the_json_compatible_form() -> None:
    key = uuid4()
    person = Private(name="a", password_hash="h")
    assert person.dump() == {"name": "a", "password_hash": "h"}
    assert Tag(label="x", weight=1).dump() == {"label": "x", "weight": 1}
    assert isinstance(key, type(key))


def test_unserialisable_objects_still_fail_clearly() -> None:
    with pytest.raises(TypeError, match="not JSON serializable"):
        core.encode_json({"x": object()})
