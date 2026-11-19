"""Generic schemas whose annotations are strings."""

from __future__ import annotations

from hinoki import Schema


class Box[T](Schema):
    item: T
    spare: list[T] | None = None


def test_string_annotations_are_resolved_with_the_parameters() -> None:
    box = Box[int].validate({"item": 1, "spare": [2, 3]})
    assert box.item == 1
    assert box.spare == [2, 3]
    assert Box[str].specs()["item"].ftype.kind == "str"
