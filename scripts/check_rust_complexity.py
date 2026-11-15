"""Fail when Rust function or closure cognitive complexity exceeds the budget."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

LIMIT = 15
FUNCTION_KINDS = {"function", "closure"}


def violations(document: dict[str, Any], limit: int = LIMIT) -> list[str]:
    """Return diagnostics for functions and closures over the configured limit."""
    found: list[str] = []
    source = document.get("name", "<unknown>")
    _visit_spaces(document.get("spaces", []), source, limit, found)
    return found


def _visit_spaces(
    spaces: list[dict[str, Any]], source: str, limit: int, found: list[str]
) -> None:
    for space in spaces:
        if space.get("kind") in FUNCTION_KINDS:
            score = space.get("metrics", {}).get("cognitive", {}).get("sum")
            if isinstance(score, int | float) and score > limit:
                found.append(
                    f"{source}:{space.get('start_line', '?')}: "
                    f"{space.get('name', '<unnamed>')} cognitive complexity "
                    f"{score:g} exceeds {limit}"
                )
        _visit_spaces(space.get("spaces", []), source, limit, found)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "metrics", type=Path, help="JSON output directory from rust-code-analysis-cli"
    )
    args = parser.parse_args()
    files = sorted(args.metrics.rglob("*.json"))
    if not args.metrics.is_dir() or not files:
        parser.error(f"no Rust metric JSON files found under {args.metrics}")
    errors = [
        issue
        for path in files
        for issue in violations(json.loads(path.read_text(encoding="utf-8")))
    ]
    if errors:
        print("\n".join(errors))
        return 1
    print(f"Rust cognitive complexity is within the limit ({LIMIT}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
