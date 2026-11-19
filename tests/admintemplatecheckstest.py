"""Application component overrides are checked when the admin is built."""

from pathlib import Path

import pytest

from hinoki.admin import Admin
from hinoki.admin.templates import UI, TemplateError, Templates

BUILT_IN = UI / "components"


def override(root: Path, name: str, source: str) -> Path:
    (root / "components").mkdir(exist_ok=True)
    (root / "components" / f"{name}.html").write_text(source, encoding="utf-8")
    return root


def test_an_override_that_keeps_its_placeholders_is_accepted(tmp_path: Path) -> None:
    source = (BUILT_IN / "form.html").read_text(encoding="utf-8")
    templates = Templates(override(tmp_path, "form", source + "<!-- mine -->"))
    assert "<!-- mine -->" in templates.sources["form"]


def test_an_override_may_drop_a_placeholder_that_is_not_csrf(tmp_path: Path) -> None:
    source = '<form method="post"><input name="csrf" value="{{ csrf }}"></form>'
    assert "csrf" in Templates(override(tmp_path, "confirm", source)).sources["confirm"]


@pytest.mark.parametrize("name", ["form", "table", "confirm", "auth"])
def test_an_override_without_the_csrf_token_is_refused(
    tmp_path: Path, name: str
) -> None:
    with pytest.raises(TemplateError, match=f"{name}.*csrf"):
        Templates(override(tmp_path, name, "<form method='post'></form>"))


def test_an_override_with_a_placeholder_the_component_never_gets(
    tmp_path: Path,
) -> None:
    source = (BUILT_IN / "form.html").read_text(encoding="utf-8")
    with pytest.raises(TemplateError, match=r"form.*no_such_value"):
        Templates(override(tmp_path, "form", source + "{{ no_such_value }}"))


def test_a_plain_placeholder_cannot_become_raw(tmp_path: Path) -> None:
    source = (BUILT_IN / "form.html").read_text(encoding="utf-8")
    with pytest.raises(TemplateError, match=r"form.*csrf"):
        Templates(override(tmp_path, "form", source + "{{{ csrf }}}"))


def test_a_file_that_names_no_component_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TemplateError, match="tabel"):
        Templates(override(tmp_path, "tabel", "<table></table>"))


def test_the_admin_refuses_a_bad_override_when_it_is_created(tmp_path: Path) -> None:
    override(tmp_path, "form", "<form method='post'></form>")
    with pytest.raises(TemplateError, match="csrf"):
        Admin(templates=str(tmp_path), site="Checks")
