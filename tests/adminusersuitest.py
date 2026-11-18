"""The admin's Users section: a superuser manages the other users."""

import re
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlencode

import pytest

from hinoki import Access, Hinoki
from hinoki.admin import Admin
from hinoki.admin.users import (
    AdminAccounts,
    AdminGrant,
    AdminSession,
    AdminUser,
    migrations_path,
)
from hinoki.auth import BaseAuth
from hinoki.crypto import PasswordHasher
from hinoki.db import Database, DatabaseModule
from hinoki.errors import RegistrationError
from hinoki.orm import Model, migrator, pk, use_database
from hinoki.settings import AdminSettings, AppSettings, Secret
from hinoki.testing import ASGIClient, Response

PASSWORD = "correct horse battery"  # noqa: S105 - a fixture, not a credential
OTHER = "another long password"
FORM = {"content-type": "application/x-www-form-urlencoded"}


class Nobody(BaseAuth[str]):
    async def authenticate(self, credentials: object) -> None:
        return None


class UiNote(Model, table="ui_notes"):
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


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[tuple[ASGIClient, Database]]:
    url = f"sqlite://{tmp_path / 'ui.sqlite3'}"
    database = await Database.connect(url, max_size=2)
    await migrator.apply(database, migrations_path(), namespace="admin")
    await database.execute(
        "CREATE TABLE ui_notes (id integer PRIMARY KEY, title text NOT NULL)"
    )
    site = Admin(users=accounts())
    site.register(UiNote)
    app = Hinoki(
        settings=AppSettings(admin=AdminSettings(secret=Secret("s" * 40))),
        include=[site],
        modules=[DatabaseModule(url, max_size=2)],
    )
    with use_database(database):
        async with ASGIClient(app) as client:
            yield client, database
    database.close()


async def sign_in(
    client: ASGIClient, username: str, password: str = PASSWORD
) -> dict[str, str]:
    page = await client.get("/admin/login")
    csrf = token_of(page)
    reply = await client.post(
        "/admin/login",
        body=urlencode(
            {"username": username, "password": password, "csrf": csrf}
        ).encode(),
        headers={**FORM, "cookie": f"hinoki_csrf={csrf}"},
    )
    assert reply.status == 303, reply.body
    return {
        "cookie": f"hinoki_admin_session={cookie_of(reply, 'hinoki_admin_session')}"
    }


async def post(
    client: ASGIClient,
    path: str,
    headers: dict[str, str],
    page_path: str,
    *pairs: tuple[str, str],
) -> Response:
    """POST a form with the CSRF token of the page at ``page_path``."""
    csrf = token_of(await client.get(page_path, headers=headers))
    fields = [("csrf", csrf), *pairs]
    return await client.post(
        path,
        body=urlencode(fields).encode(),
        headers={**FORM, "cookie": f"{headers['cookie']}; hinoki_csrf={csrf}"},
    )


async def test_only_a_superuser_sees_and_opens_the_users_section(
    world: tuple[ASGIClient, Database],
) -> None:
    client, _ = world
    service = accounts()
    await service.create_user("root", PASSWORD, superuser=True)
    staff = await service.create_user("staff", PASSWORD)
    await service.set_grants(
        staff, {("users", "view"), ("users", "change"), ("ui-notes", "view")}
    )

    root = await sign_in(client, "root")
    member = await sign_in(client, "staff")

    home = await client.get("/admin", headers=root)
    assert "/admin/users" in str(home.body)
    assert (await client.get("/admin/users", headers=root)).status == 200
    assert "/admin/users" not in str((await client.get("/admin", headers=member)).body)
    for path in ("/admin/users", "/admin/users/add"):
        assert (
            await client.get(path, headers=member)
        ).status == 404  # a grant is not enough


async def test_the_form_asks_for_a_password_and_lists_the_permissions_of_other_models(
    world: tuple[ASGIClient, Database],
) -> None:
    client, _ = world
    await accounts().create_user("root", PASSWORD, superuser=True)
    root = await sign_in(client, "root")

    page = (await client.get("/admin/users/add", headers=root)).body

    assert 'type="password"' in page and 'autocomplete="new-password"' in page
    for action in ("view", "add", "change", "delete"):
        assert f'value="ui-notes:{action}"' in page
    assert 'value="users:' not in page  # users are not delegated
    assert "password_hash" not in page


async def test_a_superuser_creates_a_user_with_permissions(
    world: tuple[ASGIClient, Database],
) -> None:
    client, _ = world
    await accounts().create_user("root", PASSWORD, superuser=True)
    root = await sign_in(client, "root")

    created = await post(
        client,
        "/admin/users/add",
        root,
        "/admin/users/add",
        ("username", "Bob"),
        ("password", OTHER),
        ("is_active", "on"),
        ("grants", "ui-notes:view"),
        ("grants", "ui-notes:add"),
    )

    assert created.status == 303
    bob = await AdminUser.aget(username="bob")
    assert (bob.is_active, bob.is_superuser) == (True, False)
    assert await accounts().grants(bob.id) == {
        ("ui-notes", "view"),
        ("ui-notes", "add"),
    }
    member = await sign_in(client, "bob", OTHER)
    assert (await client.get("/admin/ui-notes", headers=member)).status == 200
    assert (await client.get("/admin/ui-notes/add", headers=member)).status == 200
    assert (await client.get("/admin/users", headers=member)).status == 404


@pytest.mark.parametrize(
    ("pairs", "field", "message"),
    [
        ((("username", "bob"),), "password", "required"),
        ((("username", "bob"), ("password", "short")), "password", "at least 12"),
        ((("username", "bad name"), ("password", OTHER)), "username", "username is"),
        ((("username", "root"), ("password", OTHER)), "username", "already taken"),
    ],
)
async def test_creating_a_user_reports_what_is_wrong_and_creates_nothing(
    world: tuple[ASGIClient, Database],
    pairs: tuple[tuple[str, str], ...],
    field: str,
    message: str,
) -> None:
    client, _ = world
    await accounts().create_user("root", PASSWORD, superuser=True)
    root = await sign_in(client, "root")

    reply = await post(client, "/admin/users/add", root, "/admin/users/add", *pairs)

    assert reply.status == 422 and message in str(reply.body)
    assert await AdminUser.all().acount() == 1
    assert OTHER not in str(reply.body)  # a password is never written back


async def test_editing_keeps_the_password_unless_one_is_typed(
    world: tuple[ASGIClient, Database],
) -> None:
    client, _ = world
    service = accounts()
    await service.create_user("root", PASSWORD, superuser=True)
    bob = await service.create_user("bob", PASSWORD)
    await service.set_grants(bob, {("ui-notes", "view")})
    bob_session = await service.open_session(bob)
    root = await sign_in(client, "root")
    page = f"/admin/users/{bob.id}"

    kept = await post(
        client,
        page,
        root,
        page,
        ("username", "bob"),
        ("is_active", "on"),
        ("grants", "ui-notes:view"),
        ("grants", "ui-notes:change"),
    )
    assert kept.status == 303
    assert await service.grants(bob.id) == {
        ("ui-notes", "view"),
        ("ui-notes", "change"),
    }
    assert await service.verify("bob", PASSWORD) is not None
    assert await service.user_for(bob_session) is not None

    changed = await post(
        client,
        page,
        root,
        page,
        ("username", "bob"),
        ("is_active", "on"),
        ("password", OTHER),
        ("grants", "ui-notes:view"),
        ("grants", "ui-notes:change"),
    )
    assert changed.status == 303
    assert await service.verify("bob", PASSWORD) is None
    assert await service.verify("bob", OTHER) is not None
    assert (
        await service.user_for(bob_session) is None
    )  # a new password ends the sessions


async def test_deactivating_ends_the_sessions_and_blocks_sign_in(
    world: tuple[ASGIClient, Database],
) -> None:
    client, _ = world
    service = accounts()
    await service.create_user("root", PASSWORD, superuser=True)
    bob = await service.create_user("bob", PASSWORD)
    bob_session = await service.open_session(bob)
    root = await sign_in(client, "root")
    page = f"/admin/users/{bob.id}"

    reply = await post(
        client, page, root, page, ("username", "bob")
    )  # is_active unchecked

    assert reply.status == 303
    assert await service.user_for(bob_session) is None
    assert await service.verify("bob", PASSWORD) is None


async def test_a_superuser_cannot_lock_itself_out(
    world: tuple[ASGIClient, Database],
) -> None:
    client, _ = world
    me = await accounts().create_user("root", PASSWORD, superuser=True)
    root = await sign_in(client, "root")
    page = f"/admin/users/{me.id}"

    reply = await post(
        client, page, root, page, ("username", "root")
    )  # inactive, staff

    assert reply.status == 422
    assert "cannot deactivate yourself" in str(reply.body)
    assert "your own superuser status" in str(reply.body)
    stored = await AdminUser.aget(id=me.id)
    assert (stored.is_active, stored.is_superuser) == (True, True)


async def test_deleting_a_user_removes_its_sessions_and_grants_but_never_yourself(
    world: tuple[ASGIClient, Database],
) -> None:
    client, _ = world
    service = accounts()
    me = await service.create_user("root", PASSWORD, superuser=True)
    bob = await service.create_user("bob", PASSWORD)
    await service.set_grants(bob, {("ui-notes", "view")})
    await service.open_session(bob)
    root = await sign_in(client, "root")

    refused = await post(
        client, f"/admin/users/{me.id}/delete", root, f"/admin/users/{me.id}/delete"
    )
    gone = await post(
        client, f"/admin/users/{bob.id}/delete", root, f"/admin/users/{bob.id}/delete"
    )

    assert refused.status == 409 and "cannot delete yourself" in str(refused.body)
    assert gone.status == 303
    assert await AdminUser.filter(username="bob").acount() == 0
    assert await AdminGrant.all().acount() == 0
    assert await AdminSession.all().acount() == 1  # only root's own session is left


async def test_a_forged_form_changes_nothing(
    world: tuple[ASGIClient, Database],
) -> None:
    client, _ = world
    await accounts().create_user("root", PASSWORD, superuser=True)
    root = await sign_in(client, "root")

    reply = await client.post(
        "/admin/users/add",
        body=urlencode(
            {"csrf": "forged", "username": "eve", "password": OTHER}
        ).encode(),
        headers={**FORM, **root},
    )

    assert reply.status == 403 and await AdminUser.all().acount() == 1


async def test_the_admin_can_hide_the_users_section(tmp_path: Path) -> None:
    site = Admin(users=accounts(), manage_users=False, secret="s" * 40)
    site.api()

    assert "users" not in site.models


async def test_an_oversized_password_and_a_forged_permission_are_refused(
    world: tuple[ASGIClient, Database],
) -> None:
    client, _ = world
    await accounts().create_user("root", PASSWORD, superuser=True)
    root = await sign_in(client, "root")

    huge = await post(
        client,
        "/admin/users/add",
        root,
        "/admin/users/add",
        ("username", "bob"),
        ("password", "x" * 1200),
    )
    forged = await post(
        client,
        "/admin/users/add",
        root,
        "/admin/users/add",
        ("username", "bob"),
        ("password", OTHER),
        ("grants", "users:change"),  # not an offered choice
    )

    assert huge.status == 422 and "too long" in str(huge.body)
    assert forged.status == 422 and "only from the options" in str(forged.body)
    assert await AdminUser.all().acount() == 1


async def test_renaming_a_user_normalises_the_name_and_keeps_the_account(
    world: tuple[ASGIClient, Database],
) -> None:
    client, _ = world
    service = accounts()
    await service.create_user("root", PASSWORD, superuser=True)
    bob = await service.create_user("bob", PASSWORD)
    root = await sign_in(client, "root")
    page = f"/admin/users/{bob.id}"

    reply = await post(
        client, page, root, page, ("username", "  Robert "), ("is_active", "on")
    )

    assert reply.status == 303
    assert (await AdminUser.aget(id=bob.id)).username == "robert"
    assert await service.verify("robert", PASSWORD) is not None


def test_the_admin_account_tables_cannot_be_registered_as_plain_models() -> None:
    from hinoki.admin.users.models import AdminGrant, AdminSession  # noqa: PLC0415

    site = Admin(users=accounts(), secret="s" * 40)

    for model in (AdminUser, AdminSession, AdminGrant):
        with pytest.raises(RegistrationError, match="managed by the Users section"):
            site.register(model)
    # an own site with its own identity is not affected
    plain = Admin(access=Access(auth=Nobody()), secret="s" * 40)
    plain.register(AdminUser)


async def test_the_last_active_superuser_cannot_be_demoted_deleted_or_deactivated(
    world: tuple[ASGIClient, Database],
) -> None:
    client, _ = world
    service = accounts()
    first = await service.create_user("root", PASSWORD, superuser=True)
    second = await service.create_user("second", PASSWORD, superuser=True)
    root = await sign_in(client, "root")
    page = f"/admin/users/{second.id}"

    demote = await post(
        client, page, root, page, ("username", "second"), ("is_active", "on")
    )
    assert demote.status == 303  # one superuser (root) remains
    await AdminUser.aupdate(id=second.id, is_superuser=True, returning=False)

    other = await sign_in(client, "second")
    # deactivating root is fine while another superuser exists; the last one stays
    first_page = f"/admin/users/{first.id}"
    one = await post(
        client,
        first_page,
        other,
        first_page,
        ("username", "root"),
        ("is_superuser", "on"),
    )
    assert one.status == 303 and (await AdminUser.aget(id=first.id)).is_active is False
    self_page = f"/admin/users/{second.id}"
    refused = await post(
        client, self_page, other, self_page, ("username", "second"), ("is_active", "on")
    )
    assert refused.status == 422  # you cannot demote yourself
    assert await AdminUser.filter(is_superuser=True, is_active=True).acount() == 1


def test_a_user_repr_never_carries_the_password_hash() -> None:
    user = AdminUser(username="ada", password_hash="$argon2id$secret-hash")  # noqa: S106 - a fixture

    assert "secret-hash" not in repr(user) and "ada" in repr(user)


async def test_users_forms_hash_before_the_write_and_hold_no_route_connection() -> None:
    site = Admin(users=accounts(), secret="s" * 40)
    app = Hinoki(include=[site])
    routes = {(r.method, r.path): r for r in app.routes}

    assert routes[("POST", "/admin/{model}/add")].transaction is False
    assert routes[("POST", "/admin/{model}/{key}")].transaction is False
    assert routes[("POST", "/admin/{model}/bulk")].transaction is True


async def test_the_guard_refuses_a_write_that_would_leave_no_active_superuser(
    world: tuple[ASGIClient, Database],
) -> None:
    from hinoki.admin.users import AdminUserError  # noqa: PLC0415 - only this test
    from hinoki.admin.users.useradmin import UserAdmin  # noqa: PLC0415

    _ = world
    only = await accounts().create_user("only", PASSWORD, superuser=True)
    await UserAdmin.keep_a_superuser()  # one active superuser: fine

    await accounts().set_active(only, False)  # what two racing admins could leave

    with pytest.raises(AdminUserError, match="at least one active superuser"):
        await UserAdmin.keep_a_superuser()
