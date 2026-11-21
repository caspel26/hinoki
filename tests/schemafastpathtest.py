"""The compiled acceptance check changes how fast a schema is built, never what is
accepted or how a refusal reads."""

import datetime as dt
import uuid
from typing import Annotated, Any

import pytest

from hinoki import Field, Invalid, Schema, field_normalize, object_check
from hinoki import schema as schema_module
from hinoki.errors import ValidationError


class Tiny(Schema):
    status: str


class Author(Schema):
    id: uuid.UUID
    name: Annotated[str, Field(min_length=2, max_length=5)]
    born: dt.date
    seen: dt.datetime
    score: float
    age: Annotated[int, Field(ge=0, lt=150)]
    active: bool
    nickname: str | None = None


class Post(Schema):
    title: str
    author: Author
    reviewer: Author | None = None
    tags: list[str]
    coauthors: list[Author]


class Loose(Schema):
    age: int


class Strict(Schema):
    age: Annotated[int, Field(ge=18)]


class Holder(Schema):
    inner: Strict


class Lowered(Schema):
    name: Annotated[str, Field(lower=True)]


class Trimmed(Schema):
    name: str

    @field_normalize("name")
    def trim(self, value: str) -> str:
        return value.strip()


class Even(Schema):
    n: int

    @object_check
    def even(self) -> None:
        if self.n % 2:
            raise Invalid("odd", "must be even")


AWARE = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
GOOD: dict[str, Any] = {
    "id": uuid.UUID(int=1),
    "name": "Ada",
    "born": dt.date(1815, 12, 10),
    "seen": AWARE,
    "score": 1.5,
    "age": 36,
    "active": True,
}


class Text(str):
    pass


class Whole(int):
    pass


def outcome(make: Any) -> Any:
    try:
        built = make()
    except ValidationError as exc:
        return ("error", [(d.path, d.code, d.message) for d in exc.details])
    return ("ok", built.to_dict() if hasattr(built, "to_dict") else built)


@pytest.fixture
def slow(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Run the same construction with the compiled check switched off."""

    def run(make: Any) -> Any:
        with monkeypatch.context() as patch:
            patch.setattr(schema_module, "instance_accepts", lambda *_: False)
            return outcome(make)

    return run


@pytest.fixture
def full(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    original = Schema._validate_instance.__func__  # type: ignore[attr-defined]

    def counting(cls: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return original(cls, *args, **kwargs)

    monkeypatch.setattr(Schema, "_validate_instance", classmethod(counting))
    return calls


def test_a_value_the_check_accepts_skips_the_full_validation(full: list[int]) -> None:
    Tiny(status="ok")
    assert full == []
    Author(**GOOD)
    assert full == []


def test_a_value_it_cannot_vouch_for_takes_the_full_validation(
    full: list[int],
) -> None:
    Tiny(status="é")  # non-ASCII is vouched for too
    assert full == []
    Author(**{**GOOD, "score": 3})  # an int for a float needs converting
    assert full == [1]


SCALARS = [
    "ok", "", "x" * 3, "é", "a\0b", "\ud800", Text("ok"), 5, None, True, b"ok", 1.5,
]  # fmt: skip


@pytest.mark.parametrize("value", SCALARS, ids=repr)
def test_text_fields_agree(value: Any, slow: Any) -> None:
    assert outcome(lambda: Tiny(status=value)) == slow(lambda: Tiny(status=value))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "A"),
        ("name", "Adaaaa"),
        ("name", "Ada"),
        ("name", "Åda"),
        ("age", -1),
        ("age", 0),
        ("age", 149),
        ("age", 150),
        ("age", True),
        ("age", Whole(3)),
        ("age", 2**70),
        ("age", 5.0),
        ("age", None),
        ("score", float("nan")),
        ("score", float("inf")),
        ("score", 1e308),
        ("score", 2),
        ("score", True),
        ("active", 1),
        ("active", False),
        ("id", "x"),
        ("id", uuid.UUID(int=7)),
        ("born", AWARE),
        ("born", dt.date.min),
        ("born", dt.date.max),
        ("seen", dt.datetime(2026, 1, 1)),
        ("seen", dt.datetime.max.replace(tzinfo=dt.UTC)),
        ("seen", dt.datetime.min.replace(tzinfo=dt.UTC)),
        ("seen", AWARE.astimezone(dt.timezone(dt.timedelta(hours=-23, minutes=-59)))),
        ("seen", AWARE.astimezone(dt.timezone(dt.timedelta(seconds=1)))),
        ("seen", "2026-01-01"),
        ("nickname", None),
        ("nickname", "n"),
        ("nickname", 3),
    ],
    ids=repr,
)
def test_scalar_fields_agree(field: str, value: Any, slow: Any) -> None:
    def make() -> Author:
        return Author(**{**GOOD, field: value})

    assert outcome(make) == slow(make)


def author(**changes: Any) -> Author:
    return Author(**{**GOOD, **changes})


class SubAuthor(Author):
    extra: int = 0


def test_nested_schemas_and_lists_agree(slow: Any) -> None:
    good = author()
    cases: list[dict[str, Any]] = [
        {"author": good, "tags": ["a"], "coauthors": [good, good]},
        {"author": good, "reviewer": good, "tags": [], "coauthors": []},
        {"author": good, "tags": [1], "coauthors": []},
        {"author": good, "tags": ["a", None], "coauthors": []},
        {"author": good, "tags": ("a",), "coauthors": []},
        {"author": good, "tags": ["a"], "coauthors": [good, 3]},
        {"author": {"id": 1}, "tags": ["a"], "coauthors": []},
        {"author": None, "tags": ["a"], "coauthors": []},
        {"author": SubAuthor(**GOOD), "tags": ["a"], "coauthors": []},
        {"author": good, "tags": ["a"], "coauthors": [SubAuthor(**GOOD)]},
        {"author": Tiny(status="x"), "tags": ["a"], "coauthors": []},
    ]
    for values in cases:

        def make(values: dict[str, Any] = values) -> Post:
            return Post(title="t", **values)

        assert outcome(make) == slow(make), values


def test_a_nested_instance_changed_after_it_was_built_is_judged_as_it_is_now(
    slow: Any,
) -> None:
    inner = Strict(age=30)
    object.__setattr__(inner, "age", 3)

    def make() -> Holder:
        return Holder(inner=inner)

    kind, details = outcome(make)
    assert kind == "error"
    assert details == slow(make)[1]
    assert details[0][0] == "inner.age"


def test_a_list_changed_after_the_instance_was_built_is_judged_as_it_is_now() -> None:
    good = author()
    tags = ["a"]
    post = Post(title="t", author=good, tags=tags, coauthors=[])
    tags.append(5)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        post.replace(title="u")


def test_a_looser_subclass_is_not_taken_for_its_stricter_parent() -> None:
    class Wide(Strict):
        age: int

    young = Wide(age=3)
    with pytest.raises(ValidationError) as caught:
        Holder(inner=young)
    assert caught.value.details[0].path == "inner.age"


def test_normalizers_and_checks_still_apply() -> None:
    assert Lowered(name="ADA").name == "ada"
    assert Trimmed(name="  ada ").name == "ada"
    assert Even(n=2).n == 2
    with pytest.raises(ValidationError) as caught:
        Even(n=3)
    assert caught.value.details[0].code == "odd"
