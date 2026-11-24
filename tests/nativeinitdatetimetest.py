"""Native constructor support for ``datetime`` fields preserves Python semantics."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone, tzinfo

import pytest

from hinoki import Field, Schema, core
from hinoki.errors import ValidationError
from hinoki.schema.construct import native_init_parts


class AuthorOut(Schema):
    id: int
    name: str
    joined_at: datetime


class ArticleOut(Schema):
    id: int
    title: str
    author: AuthorOut
    published_at: datetime
    updated_at: datetime | None = None


class ArticlePage(Schema):
    items: list[ArticleOut]
    generated_at: datetime


class Event(Schema):
    ts: datetime


class NullableEvent(Schema):
    ts: datetime | None = None


class DefaultEvent(Schema):
    name: str
    ts: datetime = Field(
        default_factory=lambda: datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    )


class CustomDatetime(datetime):
    calls: int = 0

    def isoformat(self, sep: str = "T", timespec: str = "auto") -> str:
        type(self).calls += 1
        return super().isoformat(sep=sep, timespec=timespec)


class CountingTz(tzinfo):
    def __init__(self, offset: timedelta | None) -> None:
        self._offset = offset
        self.calls = 0

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        self.calls += 1
        return self._offset

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return "CUSTOM"


class RaisingTz(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        raise RuntimeError("boom")

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return "BOOM"


def test_datetime_schemas_resolve_native_init_plan() -> None:
    for schema_cls in (AuthorOut, ArticleOut, ArticlePage, Event, NullableEvent):
        assert isinstance(schema_cls.__init__, core.NativeInit)
        assert native_init_parts(schema_cls) is not None
    # A non-None typed JSON decode on null proves Compiled.typed is populated.
    decoded = NullableEvent.validate_json(b'{"ts": null}')
    assert decoded == NullableEvent(ts=None)


def test_nested_datetime_schemas_construct_identically() -> None:
    joined = datetime(2025, 1, 10, 8, 15, 30, 123456, tzinfo=UTC)
    published = datetime(2026, 5, 15, 14, 30, tzinfo=timezone(timedelta(hours=2)))
    generated = datetime(
        2026, 5, 15, 16, 45, tzinfo=timezone(timedelta(hours=-5, minutes=-30))
    )

    author = AuthorOut(id=1, name="Ada", joined_at=joined)
    article = ArticleOut(
        id=10,
        title="Hinoki",
        author=author,
        published_at=published,
    )
    page = ArticlePage(items=[article], generated_at=generated)

    assert page.items[0].author.joined_at is joined
    assert page.items[0].published_at is published
    assert page.items[0].updated_at is None
    assert page.generated_at is generated
    assert article.dump(exclude_unset=True) == {
        "id": 10,
        "title": "Hinoki",
        "author": {
            "id": 1,
            "name": "Ada",
            "joined_at": "2025-01-10T08:15:30.123456+00:00",
        },
        "published_at": "2026-05-15T14:30:00+02:00",
    }


def test_default_factory_and_replace_with_datetime() -> None:
    created = DefaultEvent(name="launch")
    assert created.ts == datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    later = datetime(2026, 6, 1, 9, 0, tzinfo=timezone(timedelta(hours=2)))
    updated = created.replace(ts=later)
    assert updated.ts is later
    assert updated.name == "launch"


def test_naive_datetime_rejected_with_datetime_tz() -> None:
    naive = datetime(2026, 5, 15, 12, 0, 0)
    with pytest.raises(ValidationError) as caught:
        Event(ts=naive)
    assert caught.value.details[0].path == "ts"
    assert caught.value.details[0].code == "datetime_tz"


def test_datetime_subclass_falls_back_to_python() -> None:
    CustomDatetime.calls = 0
    sub = CustomDatetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    event = Event(ts=sub)
    assert event.ts is sub
    assert CustomDatetime.calls > 0


def test_custom_tzinfo_preserves_side_effects_and_errors() -> None:
    tz = CountingTz(timedelta(hours=1))
    aware = datetime(2026, 5, 15, 12, 0, tzinfo=tz)
    event = Event(ts=aware)
    assert event.ts is aware
    assert tz.calls > 0

    none_tz = CountingTz(None)
    with pytest.raises(ValidationError) as caught:
        Event(ts=datetime(2026, 5, 15, 12, 0, tzinfo=none_tz))
    assert caught.value.details[0].code == "datetime_tz"

    with pytest.raises(RuntimeError, match="boom"):
        Event(ts=datetime(2026, 5, 15, 12, 0, tzinfo=RaisingTz()))


@pytest.mark.parametrize(
    "offset",
    [
        timedelta(seconds=30),
        timedelta(seconds=-30),
        timedelta(hours=1, seconds=1),
        timedelta(microseconds=500),
    ],
)
def test_non_whole_minute_offset_rejected(offset: timedelta) -> None:
    dt = datetime(2026, 5, 15, 12, 0, tzinfo=timezone(offset))
    with pytest.raises(ValidationError) as caught:
        Event(ts=dt)
    assert caught.value.details[0].path == "ts"
    assert caught.value.details[0].code == "datetime"


def test_nullable_and_wrong_types() -> None:
    assert NullableEvent(ts=None).ts is None
    assert NullableEvent().ts is None

    with pytest.raises(ValidationError) as null_err:
        Event(ts=None)  # type: ignore[arg-type]
    assert null_err.value.details[0].path == "ts"

    for bad in ("2026-05-15T12:00:00Z", 123, date(2026, 5, 15)):
        with pytest.raises(ValidationError) as caught:
            Event(ts=bad)  # type: ignore[arg-type]
        assert caught.value.details[0].path == "ts"
        assert caught.value.details[0].code == "datetime"


def test_validate_and_validate_json_keep_fallback_behavior() -> None:
    # JSON decode of non-null datetime falls back to None so ordinary decode runs.
    assert Event.validate_json(b'{"ts": "2026-05-15T12:30:45Z"}') is None

    validated = Event.validate({"ts": "2026-05-15T12:30:45+02:00"})
    assert validated.ts == datetime(
        2026, 5, 15, 12, 30, 45, tzinfo=timezone(timedelta(hours=2))
    )

    # Schema.validate expects RFC 3339 strings, not ready datetime objects.
    with pytest.raises(ValidationError) as caught:
        Event.validate({"ts": datetime(2026, 5, 15, 12, 30, 45, tzinfo=UTC)})
    assert caught.value.details[0].path == "ts"
    assert caught.value.details[0].code == "datetime"

    assert NullableEvent.validate({"ts": None}) == NullableEvent(ts=None)
