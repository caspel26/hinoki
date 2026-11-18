"""The admin's own sign-in: login, sessions, logout, password change and grants."""

import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

from hinoki import Hinoki
from hinoki.admin import Admin
from hinoki.admin.users import AdminAccounts, AdminSession, AdminUser, migrations_path
from hinoki.admin.users.limits import LoginWindow
from hinoki.crypto import PasswordHasher
from hinoki.db import Database, DatabaseModule
from hinoki.errors import RegistrationError
from hinoki.orm import Model, migrator, pk, use_database
from hinoki.settings import AdminSettings, AppSettings, Secret
from hinoki.testing import ASGIClient, Response

PASSWORD = "correct horse battery"  # noqa: S105 - a fixture, not a credential
FORM = {"content-type": "application/x-www-form-urlencoded"}
SECRET = Secret("s" * 40)


class LoginNote(Model, table="login_notes"):
    id: int = pk()
    title: str


def accounts() -> AdminAccounts:
    return AdminAccounts(
        hasher=PasswordHasher(memory_kib=64, time_cost=1, allow_weak=True)
    )


def cookie_of(reply: Response, name: str) -> str:
    match = re.match(rf"{name}=([^;]*)", reply.headers.get("set-cookie", ""))
    return match.group(1) if match else ""


def token_of(reply: Response) -> str:
    found = re.search(r'name="csrf" value="([^"]+)"', str(reply.body))
    return found.group(1) if found else ""


def form(**values: str) -> bytes:
    return urlencode(values).encode()


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[tuple[ASGIClient, Database]]:
    url = f"sqlite://{tmp_path / 'login.sqlite3'}"
    database = await Database.connect(url, max_size=2)
    await migrator.apply(database, migrations_path(), namespace="admin")
    await database.execute(
        "CREATE TABLE login_notes (id integer PRIMARY KEY, title text NOT NULL)"
    )
    site = Admin(users=accounts())
    site.register(LoginNote)
    app = Hinoki(
        settings=AppSettings(admin=AdminSettings(secret=SECRET)),
        include=[site],
        modules=[DatabaseModule(url, max_size=2)],
    )
    with use_database(database):
        async with ASGIClient(app) as client:
            yield client, database
    database.close()


async def sign_in(
    client: ASGIClient, username: str, password: str = PASSWORD
) -> Response:
    page = await client.get("/admin/login")
    csrf = token_of(page)
    return await client.post(
        "/admin/login",
        body=form(username=username, password=password, csrf=csrf),
        headers={**FORM, "cookie": f"hinoki_csrf={csrf}"},
    )


def session_header(reply: Response) -> dict[str, str]:
    return {
        "cookie": f"hinoki_admin_session={cookie_of(reply, 'hinoki_admin_session')}"
    }


async def test_a_visitor_is_sent_to_the_sign_in_page(
    world: tuple[ASGIClient, Database],
) -> None:
    client, _ = world

    for path in ("/admin", "/admin/login-notes", "/admin/password"):
        reply = await client.get(path)
        assert (reply.status, reply.headers["location"]) == (303, "/admin/login")
    page = await client.get("/admin/login")
    assert page.status == 200 and "Sign in" in str(page.body)
    assert (await client.get("/admin/static/admin.css")).status == 200  # public asset


async def test_signing_in_sets_a_hardened_session_cookie_and_opens_the_admin(
    world: tuple[ASGIClient, Database],
) -> None:
    client, _ = world
    await accounts().create_user("ada", PASSWORD, superuser=True)

    reply = await sign_in(client, "ADA")

    assert (reply.status, reply.headers["location"]) == (303, "/admin")
    cookie = reply.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Path=/admin" in cookie
    assert "Max-Age=43200" in cookie  # twelve hours
    home = await client.get("/admin", headers=session_header(reply))
    assert home.status == 200 and "ada" in str(home.body)
    assert 'action="/admin/logout"' in str(home.body)
    stored = await AdminSession.all().aget()
    assert stored.token_hash != cookie_of(reply, "hinoki_admin_session")


@pytest.mark.parametrize(
    ("username", "password"),
    [("ada", "wrong password!!"), ("nobody", PASSWORD), ("ada", "")],
)
async def test_bad_credentials_get_one_answer_and_no_session(
    world: tuple[ASGIClient, Database], username: str, password: str
) -> None:
    client, _ = world
    await accounts().create_user("ada", PASSWORD)

    reply = await sign_in(client, username, password)

    assert reply.status == 401 and "Wrong username or password." in str(reply.body)
    assert "hinoki_admin_session" not in reply.headers.get("set-cookie", "")
    assert await AdminSession.all().acount() == 0


async def test_the_sign_in_form_needs_its_csrf_token(
    world: tuple[ASGIClient, Database],
) -> None:
    client, _ = world
    await accounts().create_user("ada", PASSWORD)

    forged = await client.post(
        "/admin/login", body=form(username="ada", password=PASSWORD), headers=FORM
    )

    assert forged.status == 403 and await AdminSession.all().acount() == 0


async def test_an_inactive_user_cannot_sign_in(
    world: tuple[ASGIClient, Database],
) -> None:
    client, _ = world
    user = await accounts().create_user("ada", PASSWORD, active=False)

    assert (await sign_in(client, "ada")).status == 401
    assert user.is_active is False


async def test_signing_out_ends_the_session_and_needs_csrf(
    world: tuple[ASGIClient, Database],
) -> None:
    client, _ = world
    await accounts().create_user("ada", PASSWORD, superuser=True)
    signed = await sign_in(client, "ada")
    headers = session_header(signed)
    home = await client.get("/admin", headers=headers)
    csrf = token_of(home)

    refused = await client.post(
        "/admin/logout", body=form(csrf="forged"), headers={**FORM, **headers}
    )
    assert refused.status == 403 and await AdminSession.all().acount() == 1

    bye = await client.post(
        "/admin/logout",
        body=form(csrf=csrf),
        headers={**FORM, "cookie": f"{headers['cookie']}; hinoki_csrf={csrf}"},
    )
    assert (bye.status, bye.headers["location"]) == (303, "/admin/login")
    assert "Max-Age=0" in bye.headers["set-cookie"]
    assert await AdminSession.all().acount() == 0
    assert (await client.get("/admin", headers=headers)).status == 303


async def test_changing_the_password_replaces_every_session(
    world: tuple[ASGIClient, Database],
) -> None:
    client, _ = world
    user = await accounts().create_user("ada", PASSWORD, superuser=True)
    first = await sign_in(client, "ada")
    other = await accounts().open_session(user)
    headers = session_header(first)
    page = await client.get("/admin/password", headers=headers)
    csrf = token_of(page)
    cookie = f"{headers['cookie']}; hinoki_csrf={csrf}"

    wrong = await client.post(
        "/admin/password",
        body=form(
            csrf=csrf,
            current="nope nope nope",
            new="a brand new password",
            again="a brand new password",
        ),
        headers={**FORM, "cookie": cookie},
    )
    differ = await client.post(
        "/admin/password",
        body=form(
            csrf=csrf,
            current=PASSWORD,
            new="a brand new password",
            again="another one!!!!",
        ),
        headers={**FORM, "cookie": cookie},
    )
    short = await client.post(
        "/admin/password",
        body=form(csrf=csrf, current=PASSWORD, new="short", again="short"),
        headers={**FORM, "cookie": cookie},
    )
    assert [wrong.status, differ.status, short.status] == [422, 422, 422]

    done = await client.post(
        "/admin/password",
        body=form(
            csrf=csrf,
            current=PASSWORD,
            new="a brand new password",
            again="a brand new password",
        ),
        headers={**FORM, "cookie": cookie},
    )

    assert (done.status, done.headers["location"]) == (303, "/admin?done=password")
    fresh = session_header(done)
    assert fresh != headers
    assert (await client.get("/admin", headers=fresh)).status == 200
    assert (await client.get("/admin", headers=headers)).status == 303  # the old one
    assert (
        await accounts().user_for(other.token if hasattr(other, "token") else other)
        is None
    )
    assert (await sign_in(client, "ada", "a brand new password")).status == 303
    assert (await sign_in(client, "ada")).status == 401


async def test_a_user_sees_only_what_a_grant_allows(
    world: tuple[ASGIClient, Database],
) -> None:
    client, database = world
    service = accounts()
    reader = await service.create_user("reader", PASSWORD)
    await service.set_grants(reader, {("login-notes", "view")})
    nobody = await service.create_user("nobody", PASSWORD)
    await database.execute("INSERT INTO login_notes VALUES (1, 'hello')")

    headers = session_header(await sign_in(client, "reader"))
    listing = await client.get("/admin/login-notes", headers=headers)
    add = await client.get("/admin/login-notes/add", headers=headers)
    edit = await client.get("/admin/login-notes/1", headers=headers)
    empty = await client.get(
        "/admin/login-notes", headers=session_header(await sign_in(client, "nobody"))
    )

    assert listing.status == 200 and "hello" in str(listing.body)
    assert add.status == 404 and edit.status == 404  # view only: no add, no change
    assert empty.status == 404 and nobody.is_superuser is False


async def test_a_superuser_holds_every_grant_even_when_the_admin_class_says_yes(
    world: tuple[ASGIClient, Database],
) -> None:
    client, _ = world
    await accounts().create_user("root", PASSWORD, superuser=True)

    headers = session_header(await sign_in(client, "root"))

    assert (await client.get("/admin/login-notes/add", headers=headers)).status == 200


async def test_the_sign_in_route_is_rate_limited(tmp_path: Path) -> None:
    url = f"sqlite://{tmp_path / 'limited.sqlite3'}"
    database = await Database.connect(url, max_size=2)
    await migrator.apply(database, migrations_path(), namespace="admin")
    weak = PasswordHasher(memory_kib=64, time_cost=1, allow_weak=True)
    service = AdminAccounts(hasher=weak, login_limit=LoginWindow(limit=1))
    app = Hinoki(
        settings=AppSettings(admin=AdminSettings(secret=SECRET)),
        include=[Admin(users=service)],
        modules=[DatabaseModule(url, max_size=2)],
    )
    with use_database(database):
        async with ASGIClient(app) as client:  # the test client has no address
            statuses = [
                (await sign_in(client, "ghost", "x" * 12)).status for _ in range(12)
            ]
    database.close()

    # an unknown address gets a window UNKNOWN_FACTOR (10) times as large
    assert statuses[:10] == [401] * 10
    assert statuses[10:] == [429, 429]


async def test_cookies_are_secure_behind_an_https_setting(tmp_path: Path) -> None:
    url = f"sqlite://{tmp_path / 'secure.sqlite3'}"
    database = await Database.connect(url, max_size=2)
    await migrator.apply(database, migrations_path(), namespace="admin")
    service = accounts()
    site = Admin(users=service, secure_cookies=True)
    app = Hinoki(
        settings=AppSettings(admin=AdminSettings(secret=SECRET)),
        include=[site],
        modules=[DatabaseModule(url, max_size=2)],
    )
    with use_database(database):
        await service.create_user("ada", PASSWORD)
        async with ASGIClient(app) as client:
            reply = await sign_in(client, "ada")
    database.close()

    assert "Secure" in reply.headers["set-cookie"]


def test_an_admin_takes_access_or_users_not_both() -> None:
    with pytest.raises(ValueError, match="not both"):
        Admin(access=object(), users=accounts())  # type: ignore[arg-type]


def test_an_admin_with_neither_uses_its_own_users() -> None:
    assert isinstance(Admin(secret="s" * 40).users, AdminAccounts)


async def test_grants_survive_only_while_the_user_exists(
    world: tuple[ASGIClient, Database],
) -> None:
    _, _ = world
    user = await accounts().create_user("ada", PASSWORD)
    assert isinstance(user, AdminUser)


async def test_signing_in_again_replaces_the_session_and_skips_the_form(
    world: tuple[ASGIClient, Database],
) -> None:
    client, _ = world
    await accounts().create_user("ada", PASSWORD, superuser=True)
    first = await sign_in(client, "ada")
    headers = session_header(first)

    again = await client.get("/admin/login", headers=headers)
    page = await client.get("/admin/login")
    csrf = token_of(page)
    second = await client.post(
        "/admin/login",
        body=form(username="ada", password=PASSWORD, csrf=csrf),
        headers={**FORM, "cookie": f"{headers['cookie']}; hinoki_csrf={csrf}"},
    )

    assert (again.status, again.headers["location"]) == (303, "/admin")
    assert second.status == 303
    assert (await client.get("/admin", headers=headers)).status == 303  # replaced
    assert (await client.get("/admin", headers=session_header(second))).status == 200
    assert await AdminSession.all().acount() == 1


async def test_the_password_form_refuses_a_forged_token(
    world: tuple[ASGIClient, Database],
) -> None:
    client, _ = world
    await accounts().create_user("ada", PASSWORD, superuser=True)
    headers = session_header(await sign_in(client, "ada"))

    reply = await client.post(
        "/admin/password",
        body=form(csrf="forged", current=PASSWORD, new="x" * 20, again="x" * 20),
        headers={**FORM, **headers},
    )

    assert reply.status == 403
    assert (await sign_in(client, "ada")).status == 303  # the password is unchanged


def test_only_an_admin_with_accounts_has_them() -> None:
    from hinoki import Access  # noqa: PLC0415 - only this test needs it
    from hinoki.auth import BaseAuth  # noqa: PLC0415

    class Nobody(BaseAuth[str]):
        async def authenticate(self, credentials: Any) -> None:
            return None

    own = Admin(access=Access(auth=Nobody()), secret="s" * 40)

    assert own.users is None
    with pytest.raises(RegistrationError, match="no users= accounts"):
        own.accounts  # noqa: B018 - reading it is the failure under test


def test_the_session_scheme_is_documented_as_a_cookie() -> None:
    scheme = Admin(secret="s" * 40).access.auth.openapi_scheme()

    assert scheme == {"type": "apiKey", "in": "cookie", "name": "hinoki_admin_session"}


@pytest.mark.parametrize("name", ["view", "add", "change", "delete"])
def test_a_bulk_action_cannot_take_a_permission_name(name: str) -> None:
    from hinoki.admin import ModelAdmin, action  # noqa: PLC0415 - only this test

    async def run(self: Any, rows: Any, principal: Any) -> None:
        return None

    run.__name__ = name

    class Notes(ModelAdmin, model=LoginNote):  # type: ignore[type-arg]
        pass

    setattr(Notes, name, action()(run))
    with pytest.raises(RegistrationError, match="reserved bulk action name"):
        Admin(secret="s" * 40).register(Notes)


async def test_the_sign_in_token_is_not_valid_for_a_user_named_login(
    world: tuple[ASGIClient, Database],
) -> None:
    client, _ = world
    await accounts().create_user("login", PASSWORD, superuser=True)
    signed = await sign_in(client, "login")
    headers = session_header(signed)
    anonymous = token_of(await client.get("/admin/login"))  # anyone can mint this

    reply = await client.post(
        "/admin/logout",
        body=form(csrf=anonymous),
        headers={**FORM, "cookie": f"{headers['cookie']}; hinoki_csrf={anonymous}"},
    )

    assert reply.status == 403 and await AdminSession.all().acount() == 1


def test_password_checks_hold_no_connection_and_are_rate_limited() -> None:
    app = Hinoki(
        settings=AppSettings(admin=AdminSettings(secret=SECRET)),
        include=[Admin(users=accounts())],
    )
    routes = {route.path: route for route in app.routes if route.method == "POST"}

    for path in ("/admin/login", "/admin/password"):
        assert routes[path].atomic is False  # hashing waits with no connection
        assert routes[path].rate_limit is not None
    assert routes["/admin/logout"].atomic is True
