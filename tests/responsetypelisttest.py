"""Native _ResponseTypeList validation for list[Schema] and list[Model]."""

from collections.abc import Iterator
from typing import Annotated

from hinoki import Schema, core
from hinoki.orm import Model, pk
from hinoki.pagination import CursorPage, Page
from hinoki.runtime.responsecheck import compile_response_check


class ItemOut(Schema):
    id: int
    name: str


class DetailedItemOut(ItemOut):
    extra: str


class OtherOut(Schema):
    code: str


class ItemRow(Model, table="response_type_list_items"):
    id: int = pk()
    name: str


class SpecialItemRow(ItemRow):
    pass


def test_list_of_schema_uses_native_checker_and_accepts_subclasses() -> None:
    check = compile_response_check("action", list[ItemOut])
    assert isinstance(check, core._ResponseTypeList)

    item = ItemOut(id=1, name="one")
    detailed = DetailedItemOut(id=2, name="two", extra="more")

    assert check([])
    assert check([item])
    assert check([item, detailed])
    assert check([detailed])

    assert not check((item,))
    assert not check({"id": 1, "name": "one"})
    assert not check(None)
    assert not check(item)
    assert not check([{"id": 1, "name": "one"}])
    assert not check([OtherOut(code="x")])
    assert not check([ItemRow(id=1, name="one")])
    assert not check([item, OtherOut(code="x")])
    assert not check([OtherOut(code="x"), item])


def test_list_of_annotated_schema_uses_native_checker() -> None:
    check = compile_response_check("action", list[Annotated[ItemOut, "meta"]])
    assert isinstance(check, core._ResponseTypeList)
    assert check([ItemOut(id=1, name="one")])
    assert not check([OtherOut(code="x")])


def test_list_of_model_uses_native_checker_and_accepts_subclasses() -> None:
    check = compile_response_check("action", list[ItemRow])
    assert isinstance(check, core._ResponseTypeList)

    row = ItemRow(id=1, name="one")
    special = SpecialItemRow(id=2, name="two")

    assert check([])
    assert check([row])
    assert check([row, special])

    assert not check((row,))
    assert not check({"id": 1, "name": "one"})
    assert not check(None)
    assert not check([ItemOut(id=1, name="one")])
    assert not check([row, ItemOut(id=2, name="two")])


def test_list_subclass_with_custom_iter_uses_fallback() -> None:
    events: list[str] = []
    valid = ItemOut(id=1, name="ok")

    class YieldingValid(list[object]):
        def __iter__(self) -> Iterator[object]:
            events.append("valid")
            return iter([valid])

    class YieldingInvalid(list[object]):
        def __iter__(self) -> Iterator[object]:
            events.append("invalid")
            return iter([OtherOut(code="bad")])

    check = compile_response_check("action", list[ItemOut])
    assert isinstance(check, core._ResponseTypeList)

    assert check(YieldingValid(["not-a-schema"]))
    assert not check(YieldingInvalid([valid]))
    assert events == ["valid", "invalid"]


def test_scalars_unions_and_pages_do_not_use_response_type_list() -> None:
    int_check = compile_response_check("action", list[int])
    union_check = compile_response_check("action", list[ItemOut | OtherOut])
    page_check = compile_response_check("action", Page[ItemOut])
    cursor_check = compile_response_check("action", CursorPage[ItemOut])

    assert not isinstance(int_check, core._ResponseTypeList)
    assert not isinstance(union_check, core._ResponseTypeList)
    assert not isinstance(page_check, core._ResponseTypeList)
    assert not isinstance(cursor_check, core._ResponseTypeList)

    assert int_check([1, 2])
    assert not int_check([True])
    assert not int_check([1, False])

    item = ItemOut(id=1, name="one")
    other = OtherOut(code="a")
    assert union_check([])
    assert union_check([item, other])
    assert not union_check([item, 1])

    assert page_check(Page(items=[item], page=1, page_size=10, total=1))
    assert not page_check([item])
    assert cursor_check(
        CursorPage(items=[item], next=None, previous=None, page_size=10)
    )
    assert not cursor_check([item])
