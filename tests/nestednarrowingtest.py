"""A Schema nested in another Schema's field is sent as the type the field
declares, in every lane and every encoder: a subclass instance loses the fields
its subclass added, as a top-level response does."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

import pytest

from hinoki import Field, Hinoki, Schema, XMLRenderer, computed_field, core
from hinoki.cli.routes import lanes_of
from hinoki.errors import RegistrationError
from hinoki.pagination import Page
from hinoki.routing import RouteGroup
from hinoki.runtime.kiso import KisoWorker
from hinoki.testing import ASGIClient
from tests.kisoservertest import fetch

SECRET = "s3cret-value"  # noqa: S105 - a marker, not a credential


class Public(Schema):
    id: int

    @computed_field
    def tag(self) -> str:
        return f"p{self.id}"


class Private(Public):
    secret: str

    @computed_field
    def hidden(self) -> str:
        return f"{SECRET}-computed"


def private(n: int = 1) -> Private:
    return Private(id=n, secret=SECRET)


def public(n: int = 1) -> Public:
    return Public(id=n)


def shown(n: int = 1) -> dict[str, Any]:
    return {"id": n, "tag": f"p{n}"}


class Holder(Schema):
    inner: Public


class OptionalHolder(Schema):
    inner: Public | None


class Envelope(Schema):
    items: list[Public]


class MapHolder(Schema):
    by_name: dict[str, Public]


class TupleHolder(Schema):
    pair: tuple[Public, Public]
    run: tuple[Public, ...]


class ShapesHolder(Schema):
    maybes: list[Public | None]
    grid: list[list[Public]]


class Wrapped[T](Schema):
    value: T


class GenericHolder(Schema):
    boxed: Wrapped[Public]


class Mid(Schema):
    inner: Public


class PrivateMid(Mid):
    inner: Private
    note: str


class Outer(Schema):
    mid: Mid
    mids: list[Mid]


class Level3(Schema):
    leaf: Public


class Level2(Schema):
    level3: Level3 | None


class Level1(Schema):
    level2: Level2


class PrivateLevel3(Level3):
    leaf: Private
    extra: str


class Tree(Schema):
    name: str
    children: list[Tree]


class SecretTree(Tree):
    secret: str


class Cat(Schema):
    kind: Literal["cat"]
    lives: int


class Dog(Schema):
    kind: Literal["dog"]
    barks: bool


Pet = Annotated[Cat | Dog, Field(discriminator="kind")]


class Pets(Schema):
    pet: Pet
    maybe: Annotated[Cat | Dog | None, Field(discriminator="kind")]
    several: list[Pet]
    either: Public | str


class Loose(Schema):
    anything: Any
    thing: object


class Quiet(Public):
    """Decides its own wire shape."""

    def dump(self, **_: Any) -> dict[str, Any]:
        return {"id": self.id, "quiet": True}


class QuietHolder(Schema):
    inner: Quiet


class LoudSubclass(Public):
    secret: str

    def dump(self, **_: Any) -> dict[str, Any]:
        return {"id": self.id, "secret": self.secret}


def cat() -> Cat:
    return Cat(kind="cat", lives=9)


CASES: dict[str, tuple[Any, Any, object]] = {
    # path: (return annotation, value, what is sent)
    "/holder": (Holder, Holder(inner=private()), {"inner": shown()}),
    "/holder-exact": (Holder, Holder(inner=public()), {"inner": shown()}),
    "/optional": (
        OptionalHolder,
        OptionalHolder(inner=private()),
        {"inner": shown()},
    ),
    "/optional-none": (OptionalHolder, OptionalHolder(inner=None), {"inner": None}),
    "/list": (
        Envelope,
        Envelope(items=[private(1), private(2)]),
        {"items": [shown(1), shown(2)]},
    ),
    "/list-mixed": (
        Envelope,
        Envelope(items=[public(1), private(2), public(3)]),
        {"items": [shown(1), shown(2), shown(3)]},
    ),
    "/list-empty": (Envelope, Envelope(items=[]), {"items": []}),
    "/map": (
        MapHolder,
        MapHolder(by_name={"a": private(1), "b": public(2)}),
        {"by_name": {"a": shown(1), "b": shown(2)}},
    ),
    "/tuple": (
        TupleHolder,
        TupleHolder(pair=(private(1), public(2)), run=(private(3), private(4))),
        {"pair": [shown(1), shown(2)], "run": [shown(3), shown(4)]},
    ),
    "/shapes": (
        ShapesHolder,
        ShapesHolder(
            maybes=[private(1), None, public(2)],
            grid=[[private(3)], [], [public(4), private(5)]],
        ),
        {
            "maybes": [shown(1), None, shown(2)],
            "grid": [[shown(3)], [], [shown(4), shown(5)]],
        },
    ),
    "/generic": (
        GenericHolder,
        GenericHolder(boxed=Wrapped[Public](value=private())),
        {"boxed": {"value": shown()}},
    ),
    "/deep": (
        Level1,
        Level1(
            level2=Level2(
                level3=PrivateLevel3(leaf=private(7), extra=SECRET),
            )
        ),
        {"level2": {"level3": {"leaf": shown(7)}}},
    ),
    "/deep-none": (
        Level1,
        Level1(level2=Level2(level3=None)),
        {"level2": {"level3": None}},
    ),
    "/redeclared": (
        Outer,
        Outer(
            mid=PrivateMid(inner=private(1), note=SECRET),
            mids=[PrivateMid(inner=private(2), note=SECRET), Mid(inner=public(3))],
        ),
        {
            "mid": {"inner": shown(1)},
            "mids": [{"inner": shown(2)}, {"inner": shown(3)}],
        },
    ),
    "/tree": (
        Tree,
        Tree(name="a", children=[SecretTree(name="b", children=[], secret=SECRET)]),
        {"name": "a", "children": [{"name": "b", "children": []}]},
    ),
    "/pets": (
        Pets,
        Pets(
            pet=cat(),
            maybe=cat(),
            several=[cat(), Dog(kind="dog", barks=True)],
            either=private(),
        ),
        {
            "pet": {"kind": "cat", "lives": 9},
            "maybe": {"kind": "cat", "lives": 9},
            "either": shown(),
            "several": [
                {"kind": "cat", "lives": 9},
                {"kind": "dog", "barks": True},
            ],
        },
    ),
    "/quiet": (
        QuietHolder,
        QuietHolder(inner=Quiet(id=1)),
        {"inner": {"id": 1, "quiet": True}},
    ),
    "/subclass-dump": (
        Holder,
        Holder(inner=LoudSubclass(id=1, secret=SECRET)),
        {"inner": shown()},
    ),
    "/top-level": (Public, private(), shown()),
    "/top-level-list": (list[Public], [private(1), public(2)], [shown(1), shown(2)]),
    "/top-level-redeclared": (
        Mid,
        PrivateMid(inner=private(1), note=SECRET),
        {"inner": shown(1)},
    ),
    "/top-level-redeclared-list": (
        list[Mid],
        [PrivateMid(inner=private(1), note=SECRET), Mid(inner=public(2))],
        [{"inner": shown(1)}, {"inner": shown(2)}],
    ),
    "/top-level-holders": (
        list[Holder],
        [Holder(inner=private(1)), Holder(inner=public(2))],
        [{"inner": shown(1)}, {"inner": shown(2)}],
    ),
}
# A position that declares no schema has nothing to narrow to: sent as it is.
LOOSE = Loose(anything=private(), thing=private())
FULL = {"id": 1, "secret": SECRET, "tag": "p1", "hidden": f"{SECRET}-computed"}
CASES["/loose"] = (Loose, LOOSE, {"anything": FULL, "thing": FULL})
LEAKS_BY_DESIGN = {"/loose"}


def build_app(*, validate: bool) -> Hinoki:
    group = RouteGroup("")
    for path, (annotation, value, _) in CASES.items():
        group.get(path, validate_response=validate)(handler(annotation, value))
    return Hinoki(include=[group])


def handler(annotation: Any, value: Any) -> Any:
    async def serve() -> Any:
        return value

    serve.__annotations__ = {"return": annotation}
    serve.__name__ = "serve"
    return serve


def check(bodies: dict[str, bytes]) -> None:
    assert bodies.keys() == CASES.keys()
    for path, body in bodies.items():
        if path not in LEAKS_BY_DESIGN:
            assert SECRET.encode() not in body, path
        assert json.loads(body) == CASES[path][2], path


async def bodies_over_asgi(*, validate: bool) -> dict[str, bytes]:
    async with ASGIClient(build_app(validate=validate)) as client:
        out = {}
        for path in CASES:
            reply = await client.get(path)
            assert reply.status == 200, path
            out[path] = reply.raw
        return out


async def bodies_over_kiso(*, native_dispatch: bool) -> dict[str, bytes]:
    app = build_app(validate=True)
    worker = KisoWorker(app, "127.0.0.1", 0, native_dispatch=native_dispatch)
    port = await worker.start()
    try:
        if native_dispatch:
            lanes = lanes_of(app)
            assert {lanes[id(r)][0] for r in app.routes} == {"native"}
        out = {}
        for path in CASES:
            reply = await fetch(port, "GET", path)
            assert reply.status == 200, path
            out[path] = reply.body
        return out
    finally:
        await worker.stop()


@pytest.mark.parametrize("validate", [True, False])
async def test_asgi_pipeline_sends_the_declared_fields(validate: bool) -> None:
    check(await bodies_over_asgi(validate=validate))


async def test_native_lane_sends_the_declared_fields() -> None:
    check(await bodies_over_kiso(native_dispatch=True))


async def test_python_lane_matches_native_lane_bytes() -> None:
    python = await bodies_over_kiso(native_dispatch=False)
    check(python)
    assert python == await bodies_over_kiso(native_dispatch=True)
    assert python == await bodies_over_asgi(validate=True)


@pytest.mark.parametrize("path", sorted(CASES))
def test_the_two_encoders_write_the_same_bytes(path: str) -> None:
    """`encode_json` (the response encoder) and `dump()` (the Python lane and the
    public API) agree byte for byte, and with what the field types declare."""
    _, value, expected = CASES[path]
    written = core.encode_json(value, None, False, None)
    dumped = (
        [item.dump() for item in value] if isinstance(value, list) else value.dump()
    )
    assert json.loads(written) == json.loads(json.dumps(dumped))
    assert written == json.dumps(dumped, separators=(",", ":")).encode()
    if path.startswith("/top-level"):
        return  # narrowed by the response pipeline, not by the encoder
    assert json.loads(written) == expected


def test_exact_types_are_written_unchanged() -> None:
    exact = Envelope(items=[public(n) for n in range(5)])
    assert (
        core.encode_json(exact, None, False, None)
        == json.dumps(
            {"items": [shown(n) for n in range(5)]}, separators=(",", ":")
        ).encode()
    )
    assert exact.dump() == {"items": [shown(n) for n in range(5)]}


def test_to_dict_style_dumps_narrow_too() -> None:
    held = Holder(inner=private())
    assert held.dump() == {"inner": shown()}
    assert held.dump(exclude_unset=True) == {"inner": shown()}
    assert held.dump(by_alias=False) == {"inner": shown()}
    assert core.dump_instance(held, False) == {"inner": {"id": 1}}  # no computed
    assert core.dump_for_storage(held) == {"inner": {"id": 1}}


def test_a_discriminated_union_keeps_its_members_and_a_plain_one_narrows() -> None:
    dog = Dog(kind="dog", barks=False)
    pets = Pets(pet=dog, maybe=None, several=[dog, cat()], either="text")
    sent = json.loads(core.encode_json(pets, None, False, None))
    assert sent["pet"] == {"kind": "dog", "barks": False}
    assert sent["either"] == "text"
    assert sent["several"] == [
        {"kind": "dog", "barks": False},
        {"kind": "cat", "lives": 9},
    ]


def test_a_page_cannot_be_a_field_type() -> None:
    with pytest.raises(RegistrationError):

        class Paged(Schema):
            page: Page[Public]


def test_the_openapi_document_declares_the_base_schema_only() -> None:
    app = build_app(validate=True)
    document = json.dumps(app.openapi(), sort_keys=True)
    assert "Private" not in document
    assert "secret" not in document


async def test_the_xml_renderer_narrows_too() -> None:
    group = RouteGroup("")

    @group.get("/xml", renderers=[XMLRenderer(root="r")])
    async def serve() -> Envelope:
        return Envelope(items=[private(1), public(2)])

    async with ASGIClient(Hinoki(include=[group])) as client:
        reply = await client.get("/xml")
    assert reply.status == 200
    text = reply.raw.decode()
    assert SECRET not in text
    assert text.count("<id>") == 2
    assert text.count("<tag>") == 2
