"""The admin's "Users" section: only a superuser sees it and manages the others.

Everyone else is created, changed and removed here; the first superuser comes from
``hinoki admin create-superuser``. A user that could edit users could grant itself
everything, so the section is closed to everyone who is not a superuser, whatever
grants they hold.
"""

from __future__ import annotations

from typing import Any

from hinoki.admin.modeladmin import ModelAdmin
from hinoki.admin.parts import Extra, Tone
from hinoki.admin.users.auth import EVERYTHING
from hinoki.admin.users.models import AdminGrant, AdminSession, AdminUser
from hinoki.admin.users.service import ACTIONS, AdminAccounts, AdminUserError
from hinoki.auth import Caller
from hinoki.orm.context import current_database

USERS_SLUG = "users"
ADMIN_TABLES = frozenset({AdminUser, AdminSession, AdminGrant})  # never delegated
KEEP_NOTE = "Leave empty to keep the current password. Changing it signs the user out."


class UserAdmin(ModelAdmin[AdminUser], model=AdminUser):
    slug = USERS_SLUG
    icon = "users"
    label = "User"
    columns = ("username", "is_active", "is_superuser", "last_login")
    exclude = ("password_hash",)
    readonly = ("created_at", "last_login")
    search = ("username",)
    ordering = ("username",)
    bulk_delete = False  # one at a time, so a user cannot remove itself in a batch
    badges = {  # noqa: RUF012
        "is_active": {
            True: ("Active", Tone.OK),
            False: ("Inactive", Tone.WARN),
        },
        "is_superuser": {
            True: ("Superuser", Tone.ACCENT),
            False: ("Staff", Tone.NEUTRAL),
        },
    }

    def can(
        self,
        principal: Caller[Any],
        action: str,  # noqa: ARG002
        row: AdminUser | None = None,  # noqa: ARG002
    ) -> bool:
        return EVERYTHING in principal.permissions

    @property
    def accounts(self) -> AdminAccounts:
        return self.site.accounts  # type: ignore[no-any-return]

    def extras(self) -> tuple[Extra, ...]:
        """A password, and one checkbox for each action on each other model."""
        choices = tuple(
            (f"{slug}:{action}", f"{entry.plural()}: {action}")
            for slug, entry in self.site.models.items()
            if entry.model not in ADMIN_TABLES
            for action in ACTIONS
        )
        return (
            Extra("password", "Password", "password", KEEP_NOTE),
            Extra(
                "grants",
                "Permissions",
                "choices",
                "A superuser holds every permission, whatever is checked here.",
                choices,
            ),
        )

    async def extra_initial(self, row: AdminUser) -> dict[str, Any]:
        held = await self.accounts.grants(row.id)
        return {"grants": frozenset(f"{slug}:{action}" for slug, action in held)}

    async def check(
        self, values: dict[str, Any], principal: Caller[Any], row: AdminUser | None
    ) -> dict[str, str]:
        errors = self.check_texts(values)
        if row is None and "password" not in values and "password" not in errors:
            errors["password"] = "A password is required for a new user."  # noqa: S105
        name = values.get("username")
        taken = name and (row is None or name != row.username)
        if taken and await AdminUser.filter(username=name).acount():
            errors["username"] = "That username is already taken."
        if row is not None and row.id == principal.user.id:
            errors.update(self.check_self(values))
        if not errors and "password" in values:
            # Hash here, outside the write's transaction: no connection is held
            # while Argon2 runs, and ``save`` receives the finished hash.
            values["hashed"] = await self.accounts.hasher.hash(values.pop("password"))
        return errors

    def check_texts(self, values: dict[str, Any]) -> dict[str, str]:
        """Normalise the username and check the password, as the accounts do."""
        errors: dict[str, str] = {}
        try:
            if "username" in values:
                values["username"] = self.accounts.clean_username(values["username"])
        except AdminUserError as error:
            errors["username"] = str(error)
        try:
            if "password" in values:
                self.accounts.check_password(values["password"])
        except AdminUserError as error:
            errors["password"] = str(error)
        return errors

    @staticmethod
    def check_self(values: dict[str, Any]) -> dict[str, str]:
        """A user cannot lock itself out of the admin."""
        errors: dict[str, str] = {}
        if values.get("is_active") is False:
            errors["is_active"] = "You cannot deactivate yourself."
        if values.get("is_superuser") is False:
            errors["is_superuser"] = "You cannot remove your own superuser status."
        return errors

    async def save(
        self,
        values: dict[str, Any],
        principal: Caller[Any],  # noqa: ARG002
        row: AdminUser | None = None,
    ) -> AdminUser:
        accounts = self.accounts
        hashed = values.pop("hashed", None)
        grants = values.pop("grants", None)
        async with current_database().transaction():
            if row is None:
                user = await accounts.create_user(
                    values["username"],
                    "",
                    superuser=bool(values.get("is_superuser", False)),
                    active=bool(values.get("is_active", True)),
                    hashed=hashed,
                )
            else:
                user = await self.update(row, values)
            if hashed and row is not None:
                await accounts.set_password(user, "", hashed=hashed)
            if grants is not None:
                await accounts.set_grants(user, parse_grants(grants))
            await self.keep_a_superuser()
        return user

    async def update(self, row: AdminUser, values: dict[str, Any]) -> AdminUser:
        """The changed columns; deactivating ends the user's sessions."""
        if "is_active" in values and values["is_active"] != row.is_active:
            await self.accounts.set_active(row, values["is_active"])
        rest = {k: v for k, v in values.items() if k != "is_active"}
        if rest:
            row.assign(**rest)
            await row.asave()
        return row

    @staticmethod
    async def keep_a_superuser() -> None:
        """Inside the write's transaction: some active superuser must remain."""
        if not await AdminUser.filter(is_superuser=True, is_active=True).acount():
            raise AdminUserError("There must be at least one active superuser.")

    async def remove(self, row: AdminUser, principal: Caller[Any]) -> None:
        if row.id == principal.user.id:
            raise AdminUserError("You cannot delete yourself.")
        async with current_database().transaction():
            await AdminSession.filter(user_id=row.id).adelete()
            await AdminGrant.filter(user_id=row.id).adelete()
            await AdminUser.filter(id=row.id).adelete()
            await self.keep_a_superuser()


def parse_grants(chosen: frozenset[str]) -> frozenset[tuple[str, str]]:
    """``slug:action`` strings from the form as ``(slug, action)`` pairs."""
    pairs = set()
    for item in chosen:
        slug, _, action = item.rpartition(":")
        pairs.add((slug, action))
    return frozenset(pairs)
