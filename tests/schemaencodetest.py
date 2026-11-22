"""Native serialisation must equal the Python ``encode`` it replaces."""

import json
import random
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
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


class Shouted(Schema):
    word: str

    def dump(self, **_options: object) -> dict[str, object]:
        return {"word": self.word.upper()}


class Wrapped[T](Page[T]):
    def dump(self) -> dict[str, object]:
        return {"wrapped": super().dump()}


@pytest.mark.parametrize(("indent", "sort_keys"), [(None, False), (2, True)])
def test_a_page_encodes_exactly_like_its_dump(
    indent: int | None, sort_keys: bool
) -> None:
    moment = datetime(2026, 11, 13, 9, 30, tzinfo=UTC)
    rows: list[object] = [
        Public(name="a"),
        Public(name="b", nick="n"),
        Shouted(word="hey"),
        {"at": moment, "amount": Decimal("1.50"), "tags": {"b", "a"}},
        Tag(label="x", weight=2),
    ]
    for page in (Page(rows, 2, 5, 11), Page([], 1, 5, 0), Wrapped(rows, 1, 5, 5)):
        assert core.encode_json(page, indent, sort_keys) == core.encode_json(
            encode(page), indent, sort_keys
        )
    assert json.loads(core.encode_json(Page([Shouted(word="a")], 1, 1, 1))) == {
        "items": [{"word": "A"}],
        "page": 1,
        "page_size": 1,
        "total": 1,
        "pages": 1,
    }


def test_a_page_item_still_reaches_the_default_hook() -> None:
    page = Page([{"value": object()}], 1, 1, 1)
    with pytest.raises(TypeError, match="not JSON serializable"):
        core.encode_json(page)
    assert core.encode_json(page, None, False, lambda _value: "seen") == (
        b'{"items":[{"value":"seen"}],"page":1,"page_size":1,"total":1,"pages":1}'
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
