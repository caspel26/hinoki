"""The routes send through the memory backend; nothing leaves the process."""

from collections.abc import AsyncIterator

import pytest
from mailroom.main import app

from hinoki import mail
from hinoki.testing import ASGIClient

KEY = {"x-api-key": "test-key"}


@pytest.fixture
async def client() -> AsyncIterator[ASGIClient]:
    async with ASGIClient(app) as client:
        yield client


def messages() -> list[mail.Message]:
    module = next(m for m in app.modules if isinstance(m, mail.EmailModule))
    return [sent.message for sent in module.mailer.outbox if sent.message]


async def test_sign_up_sends_the_welcome_message(client: ASGIClient) -> None:
    response = await client.post(
        "/signup", json={"email": "ada@example.com", "name": "Ada <1>"}, headers=KEY
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
            "devices": ["Laptop"],
        },
        headers=KEY,
    )

    assert response.status == 202
    (message,) = messages()
    assert message.text is not None
    assert "- Laptop" in message.text
    assert " from " not in message.text


async def test_a_notice_takes_a_dict(client: ASGIClient) -> None:
    values = {"day": "Friday", "from": "22:00", "until": "23:00"}
    response = await client.post(
        "/notices/maintenance",
        json={"email": "ada@example.com", "values": values},
        headers=KEY,
    )

    assert response.status == 202
    (message,) = messages()
    assert message.subject == "Maintenance on Friday"


async def test_a_notice_with_a_missing_value_is_refused(client: ASGIClient) -> None:
    response = await client.post(
        "/notices/maintenance",
        json={"email": "ada@example.com", "values": {"day": "Friday"}},
        headers=KEY,
    )

    assert response.status == 400
    assert response.body["error"] == "invalid_notice"
    assert messages() == []


async def test_an_unknown_notice_is_not_found(client: ASGIClient) -> None:
    response = await client.post(
        "/notices/missing",
        json={"email": "ada@example.com", "values": {}},
        headers=KEY,
    )

    assert response.status == 404


async def test_the_reset_link_is_built_by_the_server(client: ASGIClient) -> None:
    body = {"email": "ada@example.com", "name": "Ada", "devices": []}

    await client.post("/password-reset", json=body, headers=KEY)
    await client.post("/password-reset", json=body, headers=KEY)
    forged = await client.post(
        "/password-reset",
        json={**body, "link": "https://evil.example/phish"},
        headers=KEY,
    )

    first, second = messages()
    assert forged.status == 422  # a caller cannot choose the link
    for message in (first, second):
        assert message.html is not None
        assert "https://example.com/reset?token=" in message.html
    assert first.html != second.html  # a fresh token each time


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/signup", {"email": "ada@example.com", "name": "Ada"}),
        ("/password-reset", {"email": "ada@example.com", "name": "A", "devices": []}),
        ("/notices/maintenance", {"email": "ada@example.com", "values": {}}),
    ],
)
async def test_every_route_needs_the_api_key(
    client: ASGIClient, path: str, body: dict[str, object]
) -> None:
    missing = await client.post(path, json=body)
    wrong = await client.post(path, json=body, headers={"x-api-key": "nope"})

    assert (missing.status, wrong.status) == (401, 401)
    assert messages() == []


async def test_request_bounds_refuse_before_any_message_is_sent(
    client: ASGIClient,
) -> None:
    response = await client.post(
        "/signup", json={"email": "ada@example.com", "name": "x" * 81}, headers=KEY
    )
    schemas = app.openapi()["components"]["schemas"]

    assert response.status == 422
    assert "name" in str(response.body)
    assert messages() == []
    assert schemas["SignUp"]["properties"]["name"]["maxLength"] == 80
