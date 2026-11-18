"""The custom theme example of the admin guide runs as written."""

import dataclasses

import pytest

from hinoki.admin import THEMES, Admin


def test_a_theme_derived_from_a_built_in_one_styles_the_admin() -> None:
    studio = THEMES["studio"]
    forest = dataclasses.replace(
        studio,
        name="forest",
        label="Forest",
        light={
            **studio.light,
            "accent": "#1f6f50",
            "accent-hover": "#17543c",
            "bg": "#f6f8f5",
        },
        dark={
            **studio.dark,
            "accent": "#5cc79b",
            "accent-hover": "#7ad6b0",
            "bg": "#0f1a14",
        },
        layout="topbar",
        extra_css=".hn-brand-text strong { letter-spacing: .04em; }",
    )

    site = Admin(theme=forest, secret="s" * 40)

    css = site.templates.stylesheet().decode()
    assert site.theme.name == "forest"
    assert "--hn-accent:#1f6f50;" in css and "letter-spacing" in css


def test_a_theme_missing_a_token_is_refused_at_creation() -> None:
    studio = THEMES["studio"]
    broken = dataclasses.replace(studio, name="broken", light={"bg": "#fff"})

    with pytest.raises(ValueError, match="light lacks"):
        Admin(theme=broken, secret="s" * 40)
