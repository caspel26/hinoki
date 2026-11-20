"""``admin.theme`` picks the built-in theme of an admin that names none."""

import dataclasses

import pytest

from hinoki import Access, Hinoki
from hinoki.admin import Admin
from hinoki.admin.themes import THEMES
from hinoki.auth import BaseAuth
from hinoki.settings import AdminSettings, AppSettings, Secret

LONG = "s" * 40


class Nobody(BaseAuth[str]):
    async def authenticate(self, credentials: object) -> None:
        return None


def settings_with(theme: str | None) -> AppSettings:
    return AppSettings(admin=AdminSettings(secret=Secret(LONG), theme=theme))


def site(**options: object) -> Admin:
    return Admin(access=Access(auth=Nobody()), **options)  # type: ignore[arg-type]


def test_the_setting_chooses_the_theme_of_an_admin_without_one() -> None:
    admin = site()
    Hinoki(settings=settings_with("bento"), include=[admin])

    assert admin.theme is THEMES["bento"]
    assert admin.parts.t is admin.templates


def test_an_admin_keeps_the_theme_it_was_given() -> None:
    admin = site(theme="paper")
    Hinoki(settings=settings_with("bento"), include=[admin])

    assert admin.theme is THEMES["paper"]


def test_an_admin_keeps_a_custom_theme_object() -> None:
    custom = dataclasses.replace(THEMES["studio"], name="mine", label="Mine")
    admin = site(theme=custom)
    Hinoki(settings=settings_with("bento"), include=[admin])

    assert admin.theme is custom


def test_the_default_theme_is_studio() -> None:
    admin = site()
    Hinoki(settings=settings_with(None), include=[admin])

    assert admin.theme is THEMES["studio"]


def test_an_unknown_theme_name_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown theme 'neon'"):
        settings_with("neon").validate()
