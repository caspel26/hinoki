"""PEP 695 generic aliases in a return annotation: the app starts, a subclass
instance is sent as the declared class, and an alias that cannot be resolved
is a ``RegistrationError`` that names the action."""

from __future__ import annotations

import json
from typing import Any

import pytest

from hinoki import Hinoki, Schema
from hinoki.errors import RegistrationError
from hinoki.routing import RouteGroup
from hinoki.runtime.responsecheck import AliasError, alias_value
from hinoki.testing import ASGIClient

SECRET = "s3cret-alias"  # noqa: S105 - a marker, not a credential


class Public(Schema):
    id: int


class Private(Public):
    secret: str


type Ident[T] = T
type Box[T] = list[T]
type Opt[T] = T | None
type Pair[A, B] = dict[B, A]
type First[T, U] = list[T]
type Tree[T] = list[Tree[T]] | T
type Rest[*Ts] = tuple[*Ts]
type Loop = list[Loop] | Public
type Ping = list[Pong] | Public
type Pong = dict[str, Ping]
type Outer = Inner | Public
type Inner = list[Inner2]
type Inner2 = list[Inner]
type Json = dict[str, Json] | list[Json] | int
type Bad = list[Undefined]  # type: ignore[name-defined]  # noqa: F821
type BadGen[T] = list[Undefined]  # type: ignore[name-defined]  # noqa: F821
type OuterBad = dict[str, Bad]


def private(n: int = 1) -> Private:
    return Private(id=n, secret=SECRET)


def app(annotation: Any, value: Any, validate: bool) -> Hinoki:
    async def serve() -> Any:
        return value

    serve.__annotations__ = {"return": annotation}
    group = RouteGroup("")
    group.get("/x", validate_response=validate)(serve)
    return Hinoki(include=[group])


CASES: dict[str, tuple[Any, Any, object]] = {
    "ident": (Ident[Public], private(), {"id": 1}),
    "ident-in-dict": (dict[str, Ident[Public]], {"a": private()}, {"a": {"id": 1}}),
    "box": (Box[Public], [private()], [{"id": 1}]),
    "opt": (Opt[Public], private(), {"id": 1}),
    "pair": (Pair[Public, str], {"a": private()}, {"a": {"id": 1}}),
    "first": (First[Public, str], [private()], [{"id": 1}]),
}


@pytest.mark.parametrize("validate", [True, False])
@pytest.mark.parametrize("case", sorted(CASES))
async def test_generic_alias_is_narrowed(case: str, validate: bool) -> None:
    annotation, value, expected = CASES[case]
    async with ASGIClient(app(annotation, value, validate)) as client:
        reply = await client.get("/x")
    assert reply.status == 200
    assert SECRET.encode() not in reply.raw
    assert json.loads(reply.raw) == expected


def test_recursive_alias_that_holds_a_schema_is_refused_with_the_action_name() -> None:
    for alias in (Tree[Public], Loop):
        with pytest.raises(RegistrationError, match=r"serve.*recursive"):
            app(alias, [], False)


async def test_recursive_alias_without_a_schema_still_starts() -> None:
    async with ASGIClient(app(Json, {"a": [1]}, False)) as client:
        reply = await client.get("/x")
    assert json.loads(reply.raw) == {"a": [1]}


def test_recursive_generic_alias_is_refused_by_the_response_check() -> None:
    with pytest.raises(RegistrationError, match="recursive"):
        app(Tree[Public], [], True)


def test_alias_with_a_type_variable_tuple_is_refused_with_the_action_name() -> None:
    for validate in (True, False):
        with pytest.raises(RegistrationError, match=r"serve.*TypeVarTuple"):
            app(Rest[Public, int], (), validate)


def test_mutually_recursive_aliases_that_hold_a_schema_are_refused() -> None:
    with pytest.raises(RegistrationError, match=r"serve.*recursive"):
        app(Ping, [], False)


def test_an_alias_given_too_few_arguments_is_refused() -> None:
    with pytest.raises(AliasError, match="Box"):
        alias_value(Box, ())


async def test_a_cycle_that_does_not_hold_a_schema_does_not_refuse_the_alias() -> None:
    async with ASGIClient(app(Outer, private(), False)) as client:
        reply = await client.get("/x")
    assert json.loads(reply.raw) == {"id": 1}


@pytest.mark.parametrize("validate", [True, False])
@pytest.mark.parametrize(
    "annotation",
    [Bad, BadGen[Public], OuterBad],
    ids=["plain", "generic", "nested"],
)
def test_unresolved_alias_raises_registration_error_naming_action(
    annotation: Any, validate: bool
) -> None:
    with pytest.raises(RegistrationError, match=r"serve.*cannot be resolved"):
        app(annotation, [], validate)
