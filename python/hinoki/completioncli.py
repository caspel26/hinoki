"""``hinoki completion bash|zsh|fish``: a shell completion script generated from
the real parser, so commands and options never drift from the CLI.

    eval "$(hinoki completion bash)"            # ~/.bashrc
    eval "$(hinoki completion zsh)"             # ~/.zshrc, after compinit
    hinoki completion fish | source             # ~/.config/fish/config.fish
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field

SHELLS = ("bash", "zsh", "fish")


@dataclass(slots=True)
class Node:
    path: tuple[str, ...]
    help: str = ""
    options: list[str] = field(default_factory=list)
    children: list[Node] = field(default_factory=list)


def build(parser: argparse.ArgumentParser, path: tuple[str, ...] = ()) -> Node:
    node = Node(path)
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            helps = {sub.dest: sub.help or "" for sub in action._get_subactions()}
            for name, child_parser in action.choices.items():
                child = build(child_parser, (*path, name))
                child.help = helps.get(name, "")
                node.children.append(child)
        else:
            node.options.extend(action.option_strings)
    return node


def walk(node: Node) -> list[Node]:
    found = [node]
    for child in node.children:
        found.extend(walk(child))
    return found


def words(node: Node) -> str:
    return " ".join([child.path[-1] for child in node.children] + node.options)


def key(path: tuple[str, ...]) -> str:
    return "".join(f"/{part}" for part in path)


def cases(nodes: list[Node], assign: str) -> str:
    return "\n".join(
        f'    "{key(node.path)}") {assign % words(node)} ;;' for node in nodes
    )


def known(nodes: list[Node]) -> str:
    return "|".join(key(node.path) for node in nodes[1:])


def bash(root: Node) -> str:
    nodes = walk(root)
    return f"""_hinoki() {{
  local cur="${{COMP_WORDS[COMP_CWORD]}}" here="" word opts
  for word in "${{COMP_WORDS[@]:1:COMP_CWORD-1}}"; do
    case "$here/$word" in
      {known(nodes)}) here="$here/$word" ;;
    esac
  done
  case "$here" in
{cases(nodes, 'opts="%s"')}
  esac
  COMPREPLY=($(compgen -W "$opts" -- "$cur"))
}}
complete -F _hinoki hinoki
"""


def zsh(root: Node) -> str:
    nodes = walk(root)
    return f"""#compdef hinoki
_hinoki() {{
  local here="" word
  local -a opts
  for word in ${{words[2,CURRENT-1]}}; do
    case "$here/$word" in
      {known(nodes)}) here="$here/$word" ;;
    esac
  done
  case "$here" in
{cases(nodes, "opts=(%s)")}
  esac
  compadd -- $opts
}}
compdef _hinoki hinoki
"""


def quoted(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def fish_condition(node: Node) -> str:
    if not node.path:
        return "__fish_use_subcommand"
    seen = "; and ".join(f"__fish_seen_subcommand_from {part}" for part in node.path)
    names = " ".join(child.path[-1] for child in node.children)
    if names:
        return f"{seen}; and not __fish_seen_subcommand_from {names}"
    return seen


def fish_option(node: Node, option: str) -> str:
    flag = f"-l {option[2:]}" if option.startswith("--") else f"-s {option[1:]}"
    condition = "; and ".join(
        f"__fish_seen_subcommand_from {part}" for part in node.path
    )
    where = f"-n {quoted(condition)} " if condition else ""
    return f"complete -c hinoki {where}{flag}"


def fish(root: Node) -> str:
    lines = ["complete -c hinoki -f"]
    for node in walk(root):
        condition = quoted(fish_condition(node))
        lines.extend(
            f"complete -c hinoki -n {condition} -a {child.path[-1]} "
            f"-d {quoted(child.help)}"
            for child in node.children
        )
        lines.extend(fish_option(node, option) for option in node.options)
    return "\n".join(lines) + "\n"


def generate(shell: str, parser: argparse.ArgumentParser) -> str:
    root = build(parser)
    return {"bash": bash, "zsh": zsh, "fish": fish}[shell](root)


def add_arguments(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    sub = commands.add_parser(
        "completion", help="print a shell completion script (bash, zsh or fish)"
    )
    sub.add_argument("shell", choices=SHELLS)


def run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    print(generate(args.shell, parser), end="")
    return 0
