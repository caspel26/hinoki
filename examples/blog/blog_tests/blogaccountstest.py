"""The blog's password login and JWT sessions."""

from collections.abc import Sequence
from typing import Any, ClassVar

from blogworld import JWT_SECRET, PASSWORD, World

from hinoki import Caller
from hinoki.crypto import JWTAuth, JWTKey


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def login(world: World, username: str, password: str) -> tuple[int, Any]:
    reply = await world.client.post(
        "/auth/login", json={"username": username, "password": password}
    )
    return reply.status, reply.body


async def test_login_gives_a_token_that_identifies_the_user(world: World) -> None:
    status, session = await login(world, "ada", PASSWORD)
    assert status == 200 and session["token_type"] == "Bearer"
    assert session["access_token"].count(".") == 2
    me = await world.client.get("/auth/me", headers=bearer(session["access_token"]))
    assert me.body == {"username": "ada", "name": "Ada", "is_editor": True}


async def test_every_failed_login_looks_the_same(world: World) -> None:
    answers = [
        await login(world, "ada", "wrong"),
        await login(world, "nobody", PASSWORD),
        await login(world, "nopass", PASSWORD),  # a user without a password
    ]
    assert {status for status, _ in answers} == {401}
    bodies = [{k: v for k, v in b.items() if k != "request_id"} for _, b in answers]
    assert bodies[0] == bodies[1] == bodies[2]


async def test_tokens_must_be_genuine_jwts_for_this_service(world: World) -> None:
    _, session = await login(world, "ada", PASSWORD)
    head, payload, signature = session["access_token"].split(".")
    flipped = signature[:-2] + ("AA" if not signature.endswith("AA") else "BB")
    for bad in (f"{head}.{payload}.{flipped}", "ada-token", f"{head}.{payload}"):
        reply = await world.client.get("/auth/me", headers=bearer(bad))
        assert reply.status == 401, bad
    assert (await world.client.get("/auth/me")).status == 401


async def test_login_input_is_validated_and_hashes_never_leak(world: World) -> None:
    assert (await world.client.post("/auth/login", json={})).status == 422
    huge = {"username": "ada", "password": "x" * 5000}
    assert (await world.client.post("/auth/login", json=huge)).status == 422
    spec = await world.client.get("/openapi.json")
    assert "password_hash" not in str(spec.body)


class Forge(JWTAuth[None]):
    """Signs like the blog does, to prove a bad subject is refused, not a 500."""

    keys: ClassVar[Sequence[JWTKey]] = (JWTKey("blog-1", "HS256", JWT_SECRET),)
    issuer = "blog"
    audience = "blog-api"

    async def resolve_claims(self, claims: dict[str, Any]) -> Caller[None] | None:
        return None


async def test_signed_tokens_with_odd_subjects_are_refused(world: World) -> None:
    forge = Forge()
    for subject in ("not-a-uuid", "00000000-0000-0000-0000-000000000000", 5):
        token = forge.issue({"sub": subject})
        reply = await world.client.get("/auth/me", headers=bearer(token))
        assert reply.status == 401, subject


async def test_login_is_rate_limited_per_client(world: World) -> None:
    # The test client has no known address: those requests share a bucket ten
    # times as large (200). The limit runs before the body is read.
    # The bucket refills one slot every three seconds, so a slow run can let a
    # few extra requests through: send until the first refusal, with a cap.
    statuses: list[int] = []
    while len(statuses) < 260 and 429 not in statuses:
        statuses.append((await world.client.post("/auth/login", json={})).status)

    assert statuses[-1] == 429
    assert len(statuses) > 200
    assert set(statuses[:-1]) == {422}
