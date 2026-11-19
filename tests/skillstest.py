"""Optional agent skills written by ``hinoki new --skills``."""

import re
from pathlib import Path

import pytest

import hinoki.rules
from hinoki.__main__ import main
from hinoki.ai.mcp import TOOLS
from hinoki.ai.skills import SKILLS, skill_files
from hinoki.scaffold import create, files

CODES = re.compile(r"HNK-[A-Z]+-\d+")


def test_skills_are_not_written_unless_asked() -> None:
    assert not any(p.startswith(".claude/") for p in files("shop"))


def test_the_flag_writes_one_file_per_skill(tmp_path: Path) -> None:
    target = create("shop", tmp_path, skills=True)
    for name in SKILLS:
        text = (target / ".claude/skills" / name / "SKILL.md").read_text()
        assert text.startswith(f"---\nname: {name}\ndescription: ")
    assert set(skill_files("shop")) == {
        f".claude/skills/{name}/SKILL.md" for name in SKILLS
    }


def test_skills_only_cite_tools_and_commands_that_exist() -> None:
    text = "\n".join(skill_files("shop").values())
    for tool in re.findall(r"`(\w+_\w+)` tool", text):
        assert tool in TOOLS
    for command in re.findall(r"uv run hinoki (\w+)", text):
        with pytest.raises(SystemExit) as stop:
            main([command, "--help"])
        assert stop.value.code == 0


def test_skills_cite_rule_ids_that_exist() -> None:
    known = Path(hinoki.rules.__file__).read_text()
    cited = CODES.findall("\n".join(skill_files("shop").values()))
    assert cited
    assert all(f'"{code}"' in known for code in cited)


def test_the_project_name_is_filled_in() -> None:
    text = "\n".join(skill_files("shop").values())
    assert "shop.main:app" in text
    assert "{name}" not in text


def test_new_accepts_the_flag(tmp_path: Path) -> None:
    code = main(["new", "shop", "--dir", str(tmp_path), "--skills", "--no-input"])
    assert code == 0
    assert (tmp_path / "shop/.claude/skills/hinoki-fix-rule/SKILL.md").exists()
    code = main(["new", "plain", "--dir", str(tmp_path), "--no-input"])
    assert code == 0
    assert not (tmp_path / "plain/.claude").exists()
