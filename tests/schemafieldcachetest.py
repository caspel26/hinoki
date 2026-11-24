"""Parity tests for the cached JSON schema field plan and streaming encoder."""

from __future__ import annotations

import json
from typing import Annotated, Any

import pytest

from hinoki import UNSET, Field, Schema, Unset, core
from hinoki.computed import computed_field
from hinoki.pagination import Page


class TagOut(Schema):
    id: int
    name: str


class AuthorOut(Schema):
    id: int
    name: str
    bio: str | Unset = UNSET


class ArticleOut(Schema):
    id: int
    title: str
    author: AuthorOut
    tags: list[TagOut]


class ArticlePage(Schema):
    items: list[ArticleOut]
    total: int
    next_cursor: str | None = None


class OptionalFields(Schema):
    first: str | Unset = UNSET
    middle: int | Unset = UNSET
    last: bool | Unset = UNSET


class SpecialAliases(Schema):
    quoted: Annotated[str, Field(alias='say "hi"')]
    slashed: Annotated[str, Field(alias="back\\slash")]
    unicode_key: Annotated[int, Field(alias="café-日本語-😀")]
    plain: str


class ComputedArticle(Schema):
    title: str
    views: int

    @computed_field(alias='display "label"')
    def label(self) -> str:
        return f"{self.title} ({self.views})"


class BaseItem(Schema):
    id: int
    name: str


class ExtendedItem(BaseItem):
    extra: str


class CustomDumpItem(BaseItem):
    def dump(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        exclude_unset: bool = False,
        by_alias: bool = True,
    ) -> dict[str, Any]:
        return {"custom": f"{self.id}:{self.name}"}


class _OpaqueToken:
    def __init__(self, label: str) -> None:
        self.label = label


class HookCarrier(Schema):
    first: Any
    second: str


def _json_bytes(
    value: Any,
    *,
    indent: int | None = None,
    sort_keys: bool = False,
    ensure_ascii: bool = False,
) -> bytes:
    separators = (",", ": ") if indent is not None else (",", ":")
    return json.dumps(
        value,
        indent=indent,
        sort_keys=sort_keys,
        ensure_ascii=ensure_ascii,
        separators=separators,
    ).encode()


def _sample_article_page() -> ArticlePage:
    author_one = AuthorOut(id=1, name="Ada", bio="Mathematician")
    author_two = AuthorOut(id=2, name="Grace")
    articles = [
        ArticleOut(
            id=10,
            title="Notes",
            author=author_one,
            tags=[TagOut(id=1, name="math"), TagOut(id=2, name="engine")],
        ),
        ArticleOut(
            id=11,
            title="Compilers",
            author=author_two,
            tags=[],
        ),
    ]
    return ArticlePage(items=articles, total=2, next_cursor="c2")


@pytest.mark.parametrize("indent", [None, 2])
@pytest.mark.parametrize("sort_keys", [False, True])
def test_nested_article_page_byte_parity(indent: int | None, sort_keys: bool) -> None:
    page = _sample_article_page()
    expected = _json_bytes(page.dump(), indent=indent, sort_keys=sort_keys)
    # Encode twice so both cold cache population and warm cache hit are verified.
    assert core.encode_json(page, indent=indent, sort_keys=sort_keys) == expected
    assert core.encode_json(page, indent=indent, sort_keys=sort_keys) == expected


@pytest.mark.parametrize(
    "item",
    [
        OptionalFields(middle=2, last=True),
        OptionalFields(first="a", last=False),
        OptionalFields(first="a", middle=2),
        OptionalFields(),
    ],
)
@pytest.mark.parametrize("indent", [None, 2])
@pytest.mark.parametrize("sort_keys", [False, True])
def test_unset_positions_and_all_unset_parity(
    item: OptionalFields, indent: int | None, sort_keys: bool
) -> None:
    expected = _json_bytes(item.dump(), indent=indent, sort_keys=sort_keys)
    assert core.encode_json(item, indent=indent, sort_keys=sort_keys) == expected
    assert core.encode_json(item, indent=indent, sort_keys=sort_keys) == expected


@pytest.mark.parametrize("indent", [None, 2])
@pytest.mark.parametrize("sort_keys", [False, True])
@pytest.mark.parametrize("ensure_ascii", [False, True])
def test_aliases_with_special_chars_and_unicode(
    indent: int | None, sort_keys: bool, ensure_ascii: bool
) -> None:
    item = SpecialAliases(
        quoted='value "1"',
        slashed="a\\b",
        unicode_key=42,
        plain="ok",
    )
    expected = _json_bytes(
        item.dump(),
        indent=indent,
        sort_keys=sort_keys,
        ensure_ascii=ensure_ascii,
    )
    encoded = core.encode_json(
        item, indent=indent, sort_keys=sort_keys, ensure_ascii=ensure_ascii
    )
    assert encoded == expected
    assert (
        core.encode_json(
            item, indent=indent, sort_keys=sort_keys, ensure_ascii=ensure_ascii
        )
        == expected
    )


@pytest.mark.parametrize("indent", [None, 2])
@pytest.mark.parametrize("sort_keys", [False, True])
def test_computed_fields_and_aliases_parity(
    indent: int | None, sort_keys: bool
) -> None:
    article = ComputedArticle(title="Cached", views=7)
    expected = _json_bytes(article.dump(), indent=indent, sort_keys=sort_keys)
    assert core.encode_json(article, indent=indent, sort_keys=sort_keys) == expected
    assert core.encode_json(article, indent=indent, sort_keys=sort_keys) == expected


def test_subclasses_keep_separate_field_plans_and_custom_dump() -> None:
    base = BaseItem(id=1, name="base")
    extended = ExtendedItem(id=2, name="ext", extra="more")
    custom = CustomDumpItem(id=3, name="custom")

    assert core.encode_json(base) == _json_bytes(base.dump())
    assert core.encode_json(extended) == _json_bytes(extended.dump())
    assert core.encode_json(custom) == _json_bytes(custom.dump())
    # Re-encode base after subclasses to confirm no cross-class cache pollution.
    assert core.encode_json(base) == _json_bytes(base.dump())


def test_page_fast_path_and_custom_dump_fallback() -> None:
    homogeneous = Page(
        items=[BaseItem(id=1, name="one"), BaseItem(id=2, name="two")],
        page=1,
        page_size=2,
        total=2,
    )
    assert core.encode_json(homogeneous) == _json_bytes(homogeneous.dump())

    mixed = Page(
        items=[BaseItem(id=1, name="one"), CustomDumpItem(id=2, name="two")],
        page=1,
        page_size=2,
        total=2,
    )
    assert core.encode_json(mixed) == _json_bytes(mixed.dump())


def test_default_hook_mutating_later_field_sees_pre_mutation_value() -> None:
    carrier = HookCarrier(first=_OpaqueToken("tok"), second="before")

    def hook(obj: object) -> str:
        assert isinstance(obj, _OpaqueToken)
        object.__setattr__(carrier, "second", "after")
        return obj.label

    encoded = core.encode_json(carrier, default=hook)
    assert encoded == b'{"first":"tok","second":"before"}'
    assert carrier.second == "after"
