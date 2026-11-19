"""Internet address types as Model columns: stored as text, read back as objects."""

import os
from collections.abc import AsyncIterator
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from typing import Annotated
from uuid import UUID, uuid4

import pytest

from hinoki.db import Database
from hinoki.errors import FieldError, ModelValidationError, RegistrationError
from hinoki.orm import Model, field, use_database
from hinoki.orm.migrations import table_state
from hinoki.schema import Field

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
needs_database = pytest.mark.skipif(
    URL is None, reason="HINOKI_TEST_DATABASE_URL is not set"
)


class Machine(Model, table="net_machines"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    v4: IPv4Address
    v6: IPv6Address | None = None
    subnet: IPv4Network | None = None
    subnet6: IPv6Network | None = None


def test_the_columns_are_text() -> None:
    columns = table_state(Machine)["columns"]
    assert {name: columns[name]["type"] for name in ("v4", "v6", "subnet6")} == {
        "v4": "text",
        "v6": "text",
        "subnet6": "text",
    }
    assert columns["v6"]["nullable"] is True


def test_a_constraint_does_not_apply_to_an_address_column() -> None:
    with pytest.raises(RegistrationError, match="do not apply"):
        type(
            "Bad",
            (Model,),
            {
                "__annotations__": {
                    "id": UUID,
                    "ip": Annotated[IPv4Address, Field(max_length=4)],
                },
                "id": field(primary_key=True, default_factory=uuid4),
            },
            table="net_bad",
        )


def test_a_wrong_value_is_refused_before_a_write() -> None:
    with pytest.raises(ModelValidationError):
        Machine.validate_values({"v4": "not an address"})
    with pytest.raises(ModelValidationError):
        Machine.validate_values({"v4": IPv6Address("::1")})
    Machine.validate_values({"v4": IPv4Address("1.2.3.4"), "v6": None})


def test_from_row_builds_objects() -> None:
    machine = Machine.from_row(
        {
            "id": uuid4(),
            "v4": "10.0.0.1",
            "v6": "::1",
            "subnet": "10.0.0.0/8",
            "subnet6": None,
        }
    )
    assert machine.v4 == IPv4Address("10.0.0.1")
    assert machine.v6 == IPv6Address("::1")
    assert machine.subnet == IPv4Network("10.0.0.0/8")
    assert machine.subnet6 is None


def test_a_corrupt_stored_value_is_a_field_error() -> None:
    with pytest.raises(FieldError, match=r"Machine.v4.*IPv4Address"):
        Machine.from_row({"id": uuid4(), "v4": "garbage"})


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    assert URL is not None
    database = await Database.connect(URL, max_size=2, wait=0.5)
    await database.execute("DROP TABLE IF EXISTS net_machines")
    await database.execute(
        "CREATE TABLE net_machines (id uuid PRIMARY KEY, v4 text NOT NULL, "
        "v6 text, subnet text, subnet6 text)"
    )
    with use_database(database):
        yield database
    await database.execute("DROP TABLE IF EXISTS net_machines")
    database.close()


@needs_database
async def test_round_trip_create_get_update_filter(db: Database) -> None:
    machine = await Machine.acreate(
        v4=IPv4Address("192.168.1.5"), subnet=IPv4Network("192.168.1.0/24")
    )
    loaded = await Machine.aget(id=machine.id)
    assert loaded.v4 == IPv4Address("192.168.1.5")
    assert loaded.subnet == IPv4Network("192.168.1.0/24")
    assert loaded.v6 is None
    updated = await Machine.aupdate(id=machine.id, v6=IPv6Address("2001:db8::1"))
    assert updated.v6 == IPv6Address("2001:db8::1")
    found = await Machine.filter(v4=IPv4Address("192.168.1.5")).acount()
    assert found == 1


@needs_database
async def test_only_address_objects_are_written(db: Database) -> None:
    with pytest.raises(ModelValidationError):
        await Machine.acreate(v4="10.1.2.3")
    with pytest.raises(ModelValidationError):
        await Machine.acreate(v4=IPv6Address("::1"))
    with pytest.raises(ModelValidationError):
        await Machine.acreate(v4=IPv4Address("1.2.3.4"), subnet="10.0.0.0/8")
    assert await Machine.all().acount() == 0
