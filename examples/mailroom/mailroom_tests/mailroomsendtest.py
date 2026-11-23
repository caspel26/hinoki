"""The routes send through the memory backend; nothing leaves the process."""

from collections.abc import AsyncIterator

import pytest
from mailroom.main import app

from hinoki import mail
from hinoki.testing import ASGIClient


@pytest.fixture
async def client() -> AsyncIterator[ASGIClient]:
    async with ASGIClient(app) as client:
        yield client


def messages() -> list[mail.Message]:
    module = next(m for m in app.modules if isinstance(m, mail.EmailModule))
    return [sent.message for sent in module.mailer.outbox if sent.message]


async def test_sign_up_sends_the_welcome_message(client: ASGIClient) -> None:
    response = await client.post(
        "/signup", json={"email": "ada@example.com", "name": "Ada <1>"}
    )

    assert response.status == 202
    (message,) = messages()
    assert message.to == ("ada@example.com",)
    assert message.subject == "Welcome to Mailroom, Ada <1>"
    assert message.text is not None
    assert "Your trial lasts 14 days." in message.text
    assert message.html is not None
    assert "<h1>Welcome, Ada &lt;1&gt;</h1>" in message.html


async def test_password_reset_lists_the_devices(client: ASGIClient) -> None:
    response = await client.post(
        "/password-reset",
        json={
            "email": "ada@example.com",
            "name": "Ada",
            "link": "https://example.com/reset?token=t",
            "devices": ["Laptop"],
        },
    )

    assert response.status == 202
    (message,) = messages()
    assert message.text is not None
    assert "- Laptop" in message.text
    assert " from " not in message.text


async def test_a_notice_takes_a_dict(client: ASGIClient) -> None:
    values = {"day": "Friday", "from": "22:00", "until": "23:00"}
    response = await client.post(
        "/notices/maintenance", json={"email": "ada@example.com", "values": values}
    )

    assert response.status == 202
    (message,) = messages()
    assert message.subject == "Maintenance on Friday"


async def test_a_notice_with_a_missing_value_is_refused(client: ASGIClient) -> None:
    response = await client.post(
        "/notices/maintenance",
        json={"email": "ada@example.com", "values": {"day": "Friday"}},
    )

    assert response.status == 400
    assert response.body["error"] == "invalid_notice"
    assert messages() == []


async def test_an_unknown_notice_is_not_found(client: ASGIClient) -> None:
    response = await client.post(
        "/notices/missing", json={"email": "ada@example.com", "values": {}}
    )

    assert response.status == 404
