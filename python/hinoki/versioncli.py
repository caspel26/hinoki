"""Show Hinoki and runtime version information."""

from __future__ import annotations

import argparse
import importlib.metadata
import platform

import hinoki
import hinoki.core as native_core
from hinoki import jsontext


def add_arguments(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    command = commands.add_parser("version", help="show Hinoki version information")
    command.add_argument(
        "--verbose", action="store_true", help="include runtime and native-core details"
    )
    command.add_argument("--format", choices=["text", "json"], default="text")


def version_info(verbose: bool) -> dict[str, str]:
    """Return installed versions; absent optional packages are explicit."""
    info = {"hinoki": hinoki.__version__}
    if verbose:
        try:
            granian = importlib.metadata.version("granian")
        except importlib.metadata.PackageNotFoundError:
            granian = "not installed"
        info.update(
            {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "core": native_core.__file__ or "unknown",
                "granian": granian,
            }
        )
    return info


def render(info: dict[str, str], fmt: str) -> str:
    if fmt == "json":
        return jsontext.dumps(info, indent=2, sort_keys=True) + "\n"
    lines = [f"hinoki {info['hinoki']}"]
    for key in ("python", "platform", "core", "granian"):
        if key in info:
            label = "Native core" if key == "core" else key.title()
            lines.append(f"{label}: {info[key]}")
    return "\n".join(lines) + "\n"


def run_version(args: argparse.Namespace) -> int:
    info = version_info(args.verbose)
    print(render(info, args.format), end="")
    return 0
