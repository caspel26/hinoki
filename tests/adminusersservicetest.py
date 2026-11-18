"""The admin's own accounts: passwords, sessions, expiry and grants."""

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from hinoki.admin import users
from hinoki.admin.users import AdminAccounts, AdminSession, AdminUser, AdminUserError
from hinoki.crypto import PasswordHasher
from hinoki.db import Database
from hinoki.orm import migrator, use_database
from hinoki.settings import AdminSettings
from hinoki.utils import utc_now

PASSWORD = "correct horse battery"  # noqa: S105 - a fixture, not a credential
MIGRATIONS = Path(users.__file__).parent / "migrations"


class Clock:
    def __init__(self) -> None:
        self.now = utc_now()

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    connected = await Database.connect(f"sqlite://{tmp_path / 'admin.sqlite3'}")
    assert await migrator.apply(connected, MIGRATIONS, namespace="admin") == [
        "0001-initial"
    ]
    with use_database(connected):
        yield connected
    connected.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def accounts(clock: Clock) -> AdminAccounts:
    weak = PasswordHasher(memory_kib=64, time_cost=1, allow_weak=True)
    return AdminAccounts(
        AdminSettings(session_hours=2, idle_minutes=30), hasher=weak, clock=clock
    )


async def test_a_user_is_stored_with_a_hashed_password(
    database: Database, accounts: AdminAccounts
) -> None:
    user = await accounts.create_user("  Ada ", PASSWORD, superuser=True)

    assert (user.username, user.is_superuser, user.is_active) == ("ada", True, True)
    assert PASSWORD not in user.password_hash and user.password_hash.startswith(
        "$argon2id$"
    )


@pytest.mark.parametrize(
    ("username", "password", "message"),
    [
        ("", PASSWORD, "A username is"),
        ("white space", PASSWORD, "A username is"),
        ("ok", "short", "at least 12"),
        ("ok", "x" * 2000, "too long"),
    ],
)
async def test_bad_usernames_and_passwords_are_refused(
    database: Database,
    accounts: AdminAccounts,
    username: str,
    password: str,
    message: str,
) -> None:
    with pytest.raises(AdminUserError, match=message):
        await accounts.create_user(username, password)


async def test_a_username_is_unique_ignoring_case(
    database: Database, accounts: AdminAccounts
) -> None:
    await accounts.create_user("ada", PASSWORD)

    with pytest.raises(AdminUserError, match="already taken"):
        await accounts.create_user("ADA", PASSWORD)


async def test_verify_accepts_only_the_right_password_of_an_active_user(
    database: Database, accounts: AdminAccounts
) -> None:
    user = await accounts.create_user("ada", PASSWORD)

    assert (await accounts.verify("ADA", PASSWORD)) is not None
    assert await accounts.verify("ada", "another password") is None
    assert await accounts.verify("nobody", PASSWORD) is None  # same work, same answer
    assert await accounts.verify("ada", "x" * 5000) is None
    user.is_active = False
    await user.asave()
    assert await accounts.verify("ada", PASSWORD) is None


async def test_a_session_resolves_until_it_expires(
    database: Database, accounts: AdminAccounts, clock: Clock
) -> None:
    user = await accounts.create_user("ada", PASSWORD)
    token = await accounts.open_session(user, "x" * 500)

    assert (await accounts.user_for(token)) is not None
    assert (await AdminSession.all().aget()).user_agent == "x" * 200
    assert (
        await AdminSession.all().aget()
    ).token_hash != token  # only a digest is kept
    clock.advance(minutes=20)
    assert await accounts.user_for(token) is not None  # used: the idle clock restarts
    clock.advance(minutes=29)
    assert await accounts.user_for(token) is not None
    clock.advance(minutes=29)
    assert await accounts.user_for(token) is not None
    clock.advance(minutes=40)  # past the two hours since sign-in
    assert await accounts.user_for(token) is None
    assert await AdminSession.all().acount() == 0  # an ended session is deleted


async def test_an_idle_session_ends(
    database: Database, accounts: AdminAccounts, clock: Clock
) -> None:
    user = await accounts.create_user("ada", PASSWORD)
    token = await accounts.open_session(user)

    clock.advance(minutes=31)

    assert await accounts.user_for(token) is None


async def test_unknown_inactive_and_oversized_tokens_resolve_to_nobody(
    database: Database, accounts: AdminAccounts
) -> None:
    user = await accounts.create_user("ada", PASSWORD)
    token = await accounts.open_session(user)

    assert await accounts.user_for("") is None
    assert await accounts.user_for("nope") is None
    assert await accounts.user_for("t" * 5000) is None
    user.is_active = False
    await user.asave()
    assert await accounts.user_for(token) is None


async def test_closing_and_revoking_sessions(
    database: Database, accounts: AdminAccounts
) -> None:
    user = await accounts.create_user("ada", PASSWORD)
    first = await accounts.open_session(user)
    second = await accounts.open_session(user)
    third = await accounts.open_session(user)

    await accounts.close_session(first)
    await accounts.revoke_sessions(user, keep=second)

    assert await accounts.user_for(first) is None
    assert await accounts.user_for(second) is not None
    assert await accounts.user_for(third) is None
    await accounts.revoke_sessions(user)
    assert await AdminSession.all().acount() == 0


async def test_changing_the_password_ends_every_session(
    database: Database, accounts: AdminAccounts
) -> None:
    user = await accounts.create_user("ada", PASSWORD)
    token = await accounts.open_session(user)

    await accounts.set_password(user, "a different password")

    assert await accounts.user_for(token) is None
    assert await accounts.verify("ada", PASSWORD) is None
    assert await accounts.verify("ada", "a different password") is not None
    with pytest.raises(AdminUserError, match="at least 12"):
        await accounts.set_password(user, "short")


async def test_purge_removes_only_expired_sessions(
    database: Database, accounts: AdminAccounts, clock: Clock
) -> None:
    user = await accounts.create_user("ada", PASSWORD)
    await accounts.open_session(user)
    clock.advance(hours=1)
    await accounts.open_session(user)
    clock.advance(minutes=70)  # the first is past two hours, the second is not

    assert await accounts.purge_expired() == 1
    assert await AdminSession.all().acount() == 1


async def test_grants_are_replaced_as_a_set(
    database: Database, accounts: AdminAccounts
) -> None:
    user = await accounts.create_user("ada", PASSWORD)

    await accounts.set_grants(user, {("posts", "view"), ("posts", "change")})
    await accounts.set_grants(user, {("posts", "view"), ("users", "add")})

    assert await accounts.grants(user.id) == {("posts", "view"), ("users", "add")}
    with pytest.raises(AdminUserError, match="Unknown permission"):
        await accounts.set_grants(user, {("posts", "explode")})
    assert await accounts.grants(user.id) == {("posts", "view"), ("users", "add")}


async def test_deleting_a_user_cascades_sessions_and_grants(
    database: Database, accounts: AdminAccounts
) -> None:
    user = await accounts.create_user("ada", PASSWORD)
    await accounts.open_session(user)
    await accounts.set_grants(user, {("posts", "view")})
    await database.execute("PRAGMA foreign_keys = ON")

    await AdminUser.filter(id=user.id).adelete()

    assert await AdminSession.all().acount() == 0
    assert await accounts.grants(user.id) == frozenset()


def test_the_settings_bound_sessions_and_passwords() -> None:
    with pytest.raises(ValueError, match="session_hours"):
        AdminSettings(session_hours=0)
    with pytest.raises(ValueError, match="idle_minutes"):
        AdminSettings(session_hours=1, idle_minutes=61)
    with pytest.raises(ValueError, match="min_password_length"):
        AdminSettings(min_password_length=4)
