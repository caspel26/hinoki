"""``hinoki skills``: list the agent skills and add them to an existing project."""

from pathlib import Path

import pytest

from hinoki.__main__ import main
from hinoki.ai.skills import SKILLS
from hinoki.scaffold import create


def test_list_names_every_skill(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["skills", "list"]) == 0
    out = capsys.readouterr().out
    assert all(name in out for name in SKILLS)


def test_install_writes_the_skills_into_the_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = create("shop", tmp_path)
    monkeypatch.chdir(target / "shop")
    assert main(["skills", "install"]) == 0
    written = target / ".claude/skills/hinoki-fix-rule/SKILL.md"
    assert "shop.main:app" in written.read_text()
    assert "hinoki-add-endpoint" in capsys.readouterr().out


def test_install_keeps_files_it_would_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = create("shop", tmp_path, skills=True)
    path = target / ".claude/skills/hinoki-fix-rule/SKILL.md"
    path.write_text("mine")
    monkeypatch.chdir(target)
    assert main(["skills", "install"]) == 0
    assert path.read_text() == "mine"
    assert "kept" in capsys.readouterr().out
    assert main(["skills", "install", "--force"]) == 0
    assert path.read_text().startswith("---\nname: hinoki-fix-rule")


def test_install_needs_a_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["skills", "install"]) == 2
    assert "hinoki.toml" in capsys.readouterr().err
