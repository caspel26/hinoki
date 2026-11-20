"""``SecretStr`` and ``pathlib.Path`` fields of a ``Schema``: validation on both
lanes, constructed values, masking, registration errors, OpenAPI and the
request lanes."""

from __future__ import annotations

import random
from pathlib import Path, PurePosixPath
from typing import Annotated, Any

import pytest
from openapi_spec_validator import validate

from hinoki import Field, Hinoki, Schema, SecretStr, http
from hinoki.errors import MigrationError, RegistrationError, ValidationError
from hinoki.orm import Model, field
from hinoki.orm.migrations import table_state
from hinoki.runtime.kiso import KisoWorker
from hinoki.schema import encode
from hinoki.testing import ASGIClient
from tests.kisoservertest import JSON, fetch
from tests.schemadifftest import native, oracle, outcome
from tests.xmlnativelanetest import recording


class Account(Schema):
    name: str
    password: Annotated[SecretStr, Field(min_length=8)]
    token: SecretStr | None = None
    home: Path
    shares: list[Path] = Field(default_factory=list)


GOOD = {"name": "ann", "password": "correct horse", "home": "/srv/ann"}


def errors(**changes: object) -> list[tuple[str, str, str]]:
    with pytest.raises(ValidationError) as caught:
        Account.validate({**GOOD, **changes})
    return [(d.path, d.code, d.message) for d in caught.value.details]


def test_a_secret_is_built_and_never_shown() -> None:
    account = Account.validate({**GOOD, "token": "t0ken"})
    assert isinstance(account.password, SecretStr)
    assert account.password.get_secret_value() == "correct horse"
    assert account.token is not None and account.token.get_secret_value() == "t0ken"
    assert "correct horse" not in repr(account)
    assert repr(account.password) == "SecretStr('**********')"
    assert str(account.password) == "**********"
    assert f"{account.password}" == "**********"


def test_secrets_compare_by_value_and_are_hashable() -> None:
    assert SecretStr("a") == SecretStr("a")
    assert SecretStr("a") != SecretStr("b")
    assert SecretStr("a") != "a"
    assert hash(SecretStr("a")) == hash(SecretStr("a"))
    assert len(SecretStr("abc")) == 3
    assert not SecretStr("")
    with pytest.raises(TypeError):
        SecretStr(5)  # type: ignore[arg-type]


def test_a_secret_is_masked_when_dumped() -> None:
    account = Account.validate({**GOOD, "token": "t0ken"})
    dumped = account.dump()
    assert dumped["password"] == "**********"
    assert dumped["token"] == "**********"
    assert dumped["home"] == "/srv/ann"
    assert "correct horse" not in str(dumped)


def test_a_path_is_built_as_a_path_object() -> None:
    account = Account.validate({**GOOD, "shares": ["/a/b", "rel/c", "."]})
    assert account.home == Path("/srv/ann")
    assert isinstance(account.home, Path)
    assert account.shares == [Path("/a/b"), Path("rel/c"), Path(".")]
    assert account.dump()["shares"] == ["/a/b", "rel/c", "."]


def test_the_constructor_takes_the_objects_not_a_plain_string() -> None:
    account = Account(
        name="a",
        password=SecretStr("correct horse"),
        home=Path("/x"),
        token=SecretStr("t"),
    )
    assert account.password == SecretStr("correct horse")
    assert account.token == SecretStr("t")
    with pytest.raises(ValidationError):
        Account(name="a", password=SecretStr("short"), home=Path("/x"))
    with pytest.raises(ValidationError):
        Account(name="a", password="correct horse", home=Path("/x"))  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Account(
            name="a",
            password=SecretStr("correct horse"),
            home="/not-a-path-object",  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError):
        Account(
            name="a",
            password=b"correct horse",  # type: ignore[arg-type]
            home=Path("/x"),
        )


def test_a_pure_path_is_not_a_path() -> None:
    with pytest.raises(ValidationError):
        Account(name="a", password=SecretStr("correct horse"), home=PurePosixPath("/x"))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"password": "short"}, [("password", "min_length", "at least 8 characters")]),
        ({"password": 12345678}, [("password", "type", "expected a string")]),
        ({"password": None}, [("password", "null", "null is not allowed")]),
        ({"token": 5}, [("token", "type", "expected a string")]),
        ({"home": ""}, [("home", "path", "expected a file system path")]),
        ({"home": "a\x00b"}, [("home", "path", "expected a file system path")]),
        ({"home": 5}, [("home", "path", "expected a file system path")]),
        ({"home": None}, [("home", "null", "null is not allowed")]),
        (
            {"shares": ["ok", ""]},
            [("shares[1]", "path", "expected a file system path")],
        ),
    ],
)
def test_invalid_values_are_refused(
    changes: dict[str, object], expected: list[tuple[str, str, str]]
) -> None:
    assert errors(**changes) == expected


def test_the_secret_is_not_echoed_in_an_error() -> None:
    with pytest.raises(ValidationError) as caught:
        Account.validate({**GOOD, "password": "short"})
    assert "short" not in str(caught.value.details)


class Pinned(Schema):
    pin: Annotated[SecretStr, Field(pattern=r"\d{4}", max_length=4)]
    where: Annotated[Path, Field(max_length=8)]


def test_length_and_pattern_constraints_apply() -> None:
    assert Pinned.validate({"pin": "1234", "where": "/srv"}).pin == SecretStr("1234")
    with pytest.raises(ValidationError) as caught:
        Pinned.validate({"pin": "12ab", "where": "/a/very/long/path"})
    assert [(d.path, d.code) for d in caught.value.details] == [
        ("pin", "pattern"),
        ("where", "max_length"),
    ]


@pytest.mark.parametrize(
    "annotation",
    [
        Annotated[SecretStr, Field(one_of=["a"])],
        Annotated[SecretStr, Field(ge=1)],
        Annotated[Path, Field(pattern="x")],
        Annotated[Path, Field(ge=1)],
    ],
)
def test_constraints_do_not_apply(annotation: Any) -> None:
    with pytest.raises(RegistrationError, match="do not apply"):
        type("Bad", (Schema,), {"__annotations__": {"a": annotation}})


def test_a_bare_path_and_secret_are_encoded() -> None:
    assert encode(Path("/srv/a")) == "/srv/a"
    assert encode({"home": Path("/x"), "key": SecretStr("k")}) == {
        "home": "/x",
        "key": "**********",
    }


@pytest.mark.parametrize("column", [Path, SecretStr])
def test_a_model_column_of_these_types_has_no_sql_type(column: Any) -> None:
    vault = type(
        "Vault",
        (Model,),
        {
            "__annotations__": {"id": int, "item": column},
            "id": field(primary_key=True),
        },
    )
    with pytest.raises(MigrationError, match=r"Vault\.item: no SQL type for"):
        table_state(vault)


@http.post("/secret/json")
async def json_account(data: Account) -> Account:
    return data


def test_openapi_describes_both_kinds() -> None:
    app = Hinoki()
    app.include(json_account)
    document = app.openapi()
    validate(document)
    props = document["components"]["schemas"]["Account"]["properties"]
    assert props["password"] == {
        "type": "string",
        "format": "password",
        "writeOnly": True,
        "minLength": 8,
    }
    assert props["home"] == {"type": "string", "format": "path"}
    assert props["shares"]["items"] == {"type": "string", "format": "path"}


def reveal(account: Account) -> tuple[object, ...]:
    """The fields with the secrets in the clear: a secret's repr is masked."""
    token = account.token.get_secret_value() if account.token else None
    return (
        account.name,
        account.password.get_secret_value(),
        token,
        account.home,
        account.shares,
    )


def test_the_oracle_agrees_on_random_inputs() -> None:
    rng = random.Random(23)  # noqa: S311 - a test fixture, not security
    texts = ["", "a", "short", "correct horse", "a\x00b", "/", "..", "x" * 30, "é"]
    values: list[object] = [*texts, *texts, 5, None, b"x", ["a"]]
    for _ in range(800):
        data = {
            "name": "n",
            "password": rng.choice(values),
            "token": rng.choice(values),
            "home": rng.choice(values),
            "shares": [rng.choice(values) for _ in range(rng.randint(0, 3))],
        }
        got = outcome(native, Account, data)
        want = outcome(oracle, Account, data)
        if got[0] == "ok":
            got = ("ok", reveal(got[1]))
            want = ("ok", reveal(want[1]))
        assert got == want, data


BODIES = [
    b'{"name": "a", "password": "correct horse", "home": "/srv/a"}',
    b'{"name": "a", "password": "short", "home": "/srv/a"}',
    b'{"name": "a", "password": "correct horse", "home": ""}',
    b'{"name": "a", "password": "correct horse", "home": "/x", "token": "t"}',
]


async def test_the_request_lanes_match_and_never_echo_the_secret() -> None:
    app = Hinoki()
    app.include(json_account)
    worker = KisoWorker(app, "127.0.0.1", 0)
    lanes: list[str] = []
    pipeline = worker.pipeline
    pipeline.handle_native = recording(lanes, "native", pipeline.handle_native)  # type: ignore[method-assign]
    pipeline.handle_unfinished = recording(lanes, "python", pipeline.handle_unfinished)  # type: ignore[method-assign]
    port = await worker.start()
    try:
        served = [
            await fetch(
                port,
                "POST",
                "/secret/json",
                JSON + f"content-length: {len(body)}\r\n",
                body=body,
            )
            for body in BODIES
        ]
    finally:
        await worker.stop()
    assert set(lanes) <= {"native", "python"}
    async with ASGIClient(app) as client:
        for body, got in zip(BODIES, served, strict=True):
            expected = await client.post(
                "/secret/json", body=body, headers={"content-type": "application/json"}
            )
            assert got.status == expected.status, body
            if got.status == 200:
                assert got.json() == expected.body, body
                assert got.json()["password"] == "**********"
            else:
                assert got.json()["details"] == expected.body["details"], body
            assert b"correct horse" not in got.body
    assert [reply.status for reply in served] == [200, 422, 422, 200]
