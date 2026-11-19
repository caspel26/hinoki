"""``Field(multiple_of=...)`` on ``int`` and ``Decimal`` fields: validation on
both lanes, registration errors, OpenAPI and the request lanes."""

from __future__ import annotations

import random
from decimal import Decimal
from typing import Annotated, Any

import pytest
from openapi_spec_validator import validate

from hinoki import Field, Hinoki, Schema, http
from hinoki.errors import RegistrationError, ValidationError
from hinoki.runtime.rustserver import RustWorker
from hinoki.testing import ASGIClient
from tests.rustservertest import JSON, fetch
from tests.schemadifftest import native, oracle, outcome
from tests.xmlnativelanetest import recording


class Order(Schema):
    quantity: Annotated[int, Field(multiple_of=5, ge=0)]
    price: Annotated[Decimal, Field(multiple_of=Decimal("0.05"), max_digits=8)]
    whole: Annotated[Decimal, Field(multiple_of=2)] = Decimal(0)
    boxes: list[Annotated[int, Field(multiple_of=3)]] = Field(default_factory=list)


def errors(data: object) -> list[tuple[str, str, str]]:
    with pytest.raises(ValidationError) as caught:
        Order.validate(data)
    return [(d.path, d.code, d.message) for d in caught.value.details]


def test_multiples_are_accepted() -> None:
    order = Order.validate(
        {"quantity": 15, "price": "1.25", "whole": "-4", "boxes": [0, -3, 9]}
    )
    assert order.quantity == 15 and order.price == Decimal("1.25")
    assert order.whole == Decimal(-4) and order.boxes == [0, -3, 9]
    assert Order.validate({"quantity": 0, "price": 3}).price == Decimal(3)


def test_other_values_are_refused_with_the_step_in_the_message() -> None:
    assert errors({"quantity": 7, "price": "1.23", "whole": "3", "boxes": [1]}) == [
        ("quantity", "multiple_of", "must be a multiple of 5"),
        ("price", "multiple_of", "must be a multiple of 0.05"),
        ("whole", "multiple_of", "must be a multiple of 2"),
        ("boxes[0]", "multiple_of", "must be a multiple of 3"),
    ]


def test_a_failed_bound_is_reported_before_the_step() -> None:
    assert errors({"quantity": -4, "price": "1"}) == [
        ("quantity", "ge", "must be greater than or equal to 0")
    ]


class Wide(Schema):
    value: Annotated[Decimal, Field(multiple_of=Decimal("0.001"), max_digits=200)]


def test_huge_and_tiny_decimals_are_exact() -> None:
    assert Wide.validate({"value": "1" + "0" * 150 + ".002"}).value
    with pytest.raises(ValidationError):
        Wide.validate({"value": "1" + "0" * 150 + ".0021"})
    for text in ("1" + "0" * 150 + ".002", "1" + "0" * 150 + ".0021", "0.0005"):
        assert (
            outcome(native, Wide, {"value": text})[0]
            == (outcome(oracle, Wide, {"value": text})[0])
        )


def test_constructed_values_are_checked() -> None:
    assert Order(quantity=10, price=Decimal("0.10")).quantity == 10
    with pytest.raises(ValidationError):
        Order(quantity=11, price=Decimal("0.10"))


@pytest.mark.parametrize(
    "annotation",
    [
        Annotated[float, Field(multiple_of=0.5)],  # type: ignore[call-overload]
        Annotated[str, Field(multiple_of=2)],
        Annotated[int, Field(multiple_of=0)],
        Annotated[int, Field(multiple_of=-2)],
        Annotated[int, Field(multiple_of=True)],
        Annotated[int, Field(multiple_of=0.5)],  # type: ignore[call-overload]
        Annotated[int, Field(multiple_of=Decimal(2))],
        Annotated[Decimal, Field(multiple_of=Decimal("NaN"))],
        Annotated[Decimal, Field(multiple_of=Decimal("-0.5"))],
        Annotated[Decimal, Field(multiple_of=Decimal("1E+99999"))],
        Annotated[Decimal, Field(multiple_of=Decimal("1E-99999"))],
        Annotated[Decimal, Field(multiple_of=0.5)],  # type: ignore[call-overload]
    ],
)
def test_unusable_steps_are_refused_at_class_creation(annotation: Any) -> None:
    with pytest.raises(RegistrationError, match="multiple_of"):
        type("Bad", (Schema,), {"__annotations__": {"a": annotation}})


def test_the_refusal_for_float_points_at_decimal() -> None:
    with pytest.raises(RegistrationError, match="Decimal"):
        type(
            "Bad",
            (Schema,),
            {"__annotations__": {"a": Annotated[float, Field(multiple_of=0.5)]}},  # type: ignore[call-overload]
        )


def test_openapi_describes_an_integer_step_only() -> None:
    app = Hinoki()

    @http.post("/orders")
    def create(data: Order) -> Order:
        return data

    app.include(create)
    document = app.openapi()
    validate(document)
    props = document["components"]["schemas"]["Order"]["properties"]
    assert props["quantity"]["multipleOf"] == 5
    assert "multipleOf" not in props["price"]
    assert props["boxes"]["items"]["multipleOf"] == 3


def test_the_oracle_agrees_on_random_inputs() -> None:
    rng = random.Random(7)  # noqa: S311 - a test fixture, not security
    pool: list[Any] = [
        0, 5, -5, 7, 10**30, 5 * 10**30, 5 * 10**30 + 1, True, 2.5, 5.0, None, "5",
    ]  # fmt: skip
    decimals: list[Any] = [
        "0", "0.05", "0.1", "0.07", "-0.15", 3, "1" + "0" * 50 + ".05", "x", None,
        "0.050", "7" * 40,
    ]  # fmt: skip
    for _ in range(800):
        data = {
            "quantity": rng.choice(pool),
            "price": rng.choice(decimals),
            "whole": rng.choice(decimals),
            "boxes": [rng.choice(pool) for _ in range(rng.randint(0, 3))],
        }
        got = outcome(native, Order, data)
        want = outcome(oracle, Order, data)
        if got[0] == "ok":
            got = ("ok", repr(got[1]))
            want = ("ok", repr(want[1]))
        assert got == want, data


@http.post("/multiple/json")
async def json_order(data: Order) -> Order:
    return data


BODIES = [
    b'{"quantity": 10, "price": "2.50", "boxes": [3, 6]}',
    b'{"quantity": 11, "price": "2.51", "whole": "3", "boxes": [4]}',
    b'{"quantity": 9223372036854775805, "price": "0.05"}',
    b'{"quantity": 5e0, "price": 1}',
    b'{"quantity": 100000000000000000000, "price": "0.05"}',
]


async def test_the_native_lane_matches_the_python_lane() -> None:
    app = Hinoki()
    app.include(json_order)
    worker = RustWorker(app, "127.0.0.1", 0)
    lanes: list[str] = []
    pipeline = worker.pipeline
    pipeline.handle_native = recording(lanes, "native", pipeline.handle_native)  # type: ignore[method-assign]
    pipeline.handle = recording(lanes, "python", pipeline.handle)  # type: ignore[method-assign]
    port = await worker.start()
    try:
        served = [
            await fetch(
                port,
                "POST",
                "/multiple/json",
                JSON + f"content-length: {len(body)}\r\n",
                body=body,
            )
            for body in BODIES
        ]
    finally:
        await worker.stop()
    assert lanes == ["native"] * len(BODIES)
    async with ASGIClient(app) as client:
        for body, got in zip(BODIES, served, strict=True):
            expected = await client.post(
                "/multiple/json",
                body=body,
                headers={"content-type": "application/json"},
            )
            assert got.status == expected.status, body
            if got.status != 200:
                assert got.json()["details"] == expected.body["details"], body
    assert [reply.status for reply in served] == [200, 422, 200, 422, 200]
