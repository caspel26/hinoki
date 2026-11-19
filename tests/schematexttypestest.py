"""``Email``, ``Url`` and ``Slug``: ready-made text types for Schema fields and
Model columns."""

import pytest

from hinoki import Email, Schema, Slug, Url
from hinoki.errors import ModelValidationError, ValidationError
from hinoki.orm import Model, field
from hinoki.orm.migrations import table_state


class Contact(Schema):
    email: Email
    site: Url | None = None
    handle: Slug = "me"


class Person(Model, table="contact_people"):
    id: int = field(primary_key=True)
    email: Email
    site: Url | None = None


def test_valid_values_pass() -> None:
    contact = Contact.validate(
        {"email": "a@example.com", "site": "https://example.com/x?y=1", "handle": "a-b"}
    )
    assert contact.email == "a@example.com"


@pytest.mark.parametrize(
    ("body", "path", "code"),
    [
        ({"email": "nope"}, "email", "pattern"),
        ({"email": "a@example.com", "site": "ftp://x"}, "site", "pattern"),
        ({"email": "a@example.com", "handle": "Not A Slug"}, "handle", "pattern"),
    ],
)
def test_invalid_values_are_refused(body: dict[str, str], path: str, code: str) -> None:
    with pytest.raises(ValidationError) as caught:
        Contact.validate(body)
    assert [(d.path, d.code) for d in caught.value.details] == [(path, code)]


def test_a_model_column_is_text_and_checks_values() -> None:
    columns = table_state(Person)["columns"]
    assert columns["email"]["type"] == "text"
    Person.validate_values({"email": "a@example.com"})
    with pytest.raises(ModelValidationError):
        Person.validate_values({"email": "nope"})
