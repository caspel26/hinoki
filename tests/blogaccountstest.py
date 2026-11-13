"""The blog's password login and JWT sessions (needs the disposable database)."""
# ruff: noqa: S105, S106 - synthetic fixtures, not secrets

import json
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any, ClassVar

import pytest

from examples.blog.main import create_app
from examples.blog.models import User
from hinoki import Caller
from hinoki.crypto import JWTAuth, JWTKey, PasswordHasher
from hinoki.db import Database
from hinoki.orm import use_database
from tests.blogschema import drop, reset
from tests.harness import Client, Pool

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    URL is None, reason="HINOKI_TEST_DATABASE_URL is not set"
)
JSON = "application/json"
PASSWORD = "correct horse battery staple"
SECRET = "s" * 40


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    assert URL is not None
    db = await Database.connect(URL, max_size=2)
    await reset(db)
    hasher = PasswordHasher()
    with use_database(db):
        await User.acreate(
            name="Ada",
            token="ada-token",
            is_editor=True,
            username="ada",
            password_hash=await hasher.hash(PASSWORD),
        )
        await User.acreate(name="NoPassword", token="np-token", username="nopass")
        yield db
    hasher.close()
    await drop(db)
    db.close()


@pytest.fixture
async def blog(database: Database, pool: Pool) -> Client:
    assert URL is not None
    return await pool.start(create_app(URL, "t" * 40, SECRET))


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def login(blog: Client, username: str, password: str) -> tuple[int, dict]:  # type: ignore[type-arg]
    body = json.dumps({"username": username, "password": password}).encode()
    reply = await blog.request("POST", "/auth/login", body, content_type=JSON)
    return reply.status, reply.body


async def test_login_gives_a_token_that_identifies_the_user(blog: Client) -> None:
    status, session = await login(blog, "ada", PASSWORD)
    assert status == 200 and session["token_type"] == "Bearer"
    assert session["access_token"].count(".") == 2
    me = await blog.request(
        "GET",
        "/auth/me",
        headers={"authorization": f"Bearer {session['access_token']}"},
    )
    assert me.body == {"username": "ada", "name": "Ada", "is_editor": True}
    assert "password" not in json.dumps(me.body)


async def test_every_failed_login_looks_the_same(blog: Client) -> None:
    answers = [
        await login(blog, "ada", "wrong"),
        await login(blog, "nobody", PASSWORD),
        await login(blog, "nopass", PASSWORD),  # a user without a password
    ]
    assert {status for status, _ in answers} == {401}
    bodies = [{k: v for k, v in b.items() if k != "request_id"} for _, b in answers]
    assert bodies[0] == bodies[1] == bodies[2]


async def test_tokens_must_be_genuine_jwts_for_this_service(blog: Client) -> None:
    _, session = await login(blog, "ada", PASSWORD)
    token = session["access_token"]
    head, payload, signature = token.split(".")
    flipped = signature[:-2] + ("AA" if not signature.endswith("AA") else "BB")
    for bad in (f"{head}.{payload}.{flipped}", "ada-token", head + "." + payload):
        reply = await blog.request("GET", "/auth/me", headers=bearer(bad))
        assert reply.status == 401, bad
    assert (await blog.request("GET", "/auth/me")).status == 401


async def test_login_input_is_validated_and_hashes_never_leak(blog: Client) -> None:
    reply = await blog.request("POST", "/auth/login", b"{}", content_type=JSON)
    assert reply.status == 422
    huge = json.dumps({"username": "ada", "password": "x" * 5000}).encode()
    assert (
        await blog.request("POST", "/auth/login", huge, content_type=JSON)
    ).status == 422
    spec = await blog.request("GET", "/openapi.json")
    assert "password_hash" not in json.dumps(spec.body)


class Forge(JWTAuth[None]):
    """Signs like the blog does, to prove a bad subject is refused, not a 500."""

    keys: ClassVar[Sequence[JWTKey]] = (JWTKey("blog-1", "HS256", SECRET),)
    issuer = "blog"
    audience = "blog-api"

    async def resolve_claims(self, claims: dict[str, Any]) -> Caller[None] | None:
        return None


async def test_signed_tokens_with_odd_subjects_are_refused(blog: Client) -> None:
    forge = Forge()
    for subject in ("not-a-uuid", "00000000-0000-0000-0000-000000000000"):
        token = forge.issue({"sub": subject})
        reply = await blog.request("GET", "/auth/me", headers=bearer(token))
        assert reply.status == 401, subject
    reply = await blog.request(
        "GET", "/auth/me", headers=bearer(forge.issue({"sub": 5}))
    )
    assert reply.status == 401
