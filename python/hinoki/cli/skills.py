"""``hinoki skills``: list the agent skills and add them to an existing project."""

from __future__ import annotations

import argparse
import sys

from hinoki.ai.skills import SKILLS, skill_files
from hinoki.project import ProjectError, load_manifest


def add_arguments(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    group = commands.add_parser(
        "skills", help="list agent skills or add them to this project"
    )
    actions = group.add_subparsers(dest="skills_action", required=True)
    actions.add_parser("list", help="list the available skills")
    install = actions.add_parser(
        "install", help="write the skills under .claude/skills in this project"
    )
    install.add_argument(
        "--force", action="store_true", help="overwrite skills that already exist"
    )


def list_skills() -> int:
    for name, (description, _body) in SKILLS.items():
        print(f"{name}: {description}")
    return 0


def install_skills(force: bool) -> int:
    try:
        manifest = load_manifest()
    except ProjectError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    package = manifest.application.partition(":")[0].partition(".")[0]
    for relative, content in skill_files(package).items():
        path = manifest.root / relative
        if path.exists() and not force:
            print(f"kept {relative} (exists; use --force to replace)")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"wrote {relative}")
    return 0


def run(args: argparse.Namespace) -> int:
    if args.skills_action == "list":
        return list_skills()
    return install_skills(args.force)
