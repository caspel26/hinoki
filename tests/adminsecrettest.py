"""The admin signs its forms with ``admin.secret`` unless it is given its own."""

import pytest

from hinoki import Access, Hinoki, errors
from hinoki.admin import Admin
from hinoki.auth import BaseAuth
from hinoki.settings import AdminSettings, AppSettings, Secret

LONG = "s" * 40


class Nobody(BaseAuth[str]):
    async def authenticate(self, credentials: object) -> None:
        return None


def site(**options: object) -> Admin:
    return Admin(access=Access(auth=Nobody()), **options)  # type: ignore[arg-type]


def settings_with(secret: str | None) -> AppSettings:
    value = Secret(secret) if secret is not None else None
    return AppSettings(admin=AdminSettings(secret=value))


def test_the_app_secret_signs_an_admin_that_has_none() -> None:
    admin = site()
    Hinoki(settings=settings_with(LONG), include=[admin])

    assert admin.csrf is admin.csrf  # bound, and the same object each time


def test_an_admin_with_its_own_secret_keeps_it() -> None:
    admin = site(secret="own" * 20)
    own = admin.csrf

    Hinoki(settings=settings_with(LONG), include=[admin])

    assert admin.csrf is own


def test_no_secret_anywhere_stops_the_app_at_include() -> None:
    with pytest.raises(errors.RegistrationError, match=r"admin\.secret"):
        Hinoki(settings=settings_with(None), include=[site()])
    with pytest.raises(errors.RegistrationError, match=r"admin\.secret"):
        Hinoki(include=[site()])  # settings that are not AppSettings carry none


class PrefixedSettings(AppSettings, prefix="DEMO_"):
    pass


def test_the_missing_secret_message_names_the_real_variable() -> None:
    with pytest.raises(errors.RegistrationError) as caught:
        Hinoki(settings=PrefixedSettings(), include=[site()])
    assert "DEMO_ADMIN__SECRET" in str(caught.value)
    assert "DEMO__ADMIN" not in str(caught.value)


def test_the_missing_secret_message_without_a_prefix_gives_an_example() -> None:
    with pytest.raises(errors.RegistrationError) as caught:
        Hinoki(include=[site()])
    assert "<prefix>ADMIN__SECRET, for example BLOG_ADMIN__SECRET" in str(caught.value)


def test_an_unbound_admin_refuses_to_issue_tokens() -> None:
    with pytest.raises(errors.RegistrationError, match=r"admin\.secret"):
        site().csrf  # noqa: B018 - reading it is the failure under test


def test_the_app_secret_is_at_least_32_bytes() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        AdminSettings(secret=Secret("short"))
