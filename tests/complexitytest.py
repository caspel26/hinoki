"""Contract tests for the Rust complexity metric parser."""

from typing import Any

from scripts.check_rust_complexity import violations


def metric(
    kind: str,
    name: str,
    score: int,
    children: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "name": name,
        "start_line": 12,
        "metrics": {"cognitive": {"sum": score}},
        "spaces": children or [],
    }


def test_rust_complexity_accepts_limit_and_ignores_non_functions() -> None:
    document = {
        "name": "crates/example.rs",
        "spaces": [
            metric("function", "at_limit", 15),
            metric("struct", "not_a_function", 99),
        ],
    }

    assert violations(document) == []


def test_rust_complexity_reports_nested_functions_and_closures() -> None:
    document = {
        "name": "crates/example.rs",
        "spaces": [metric("function", "outer", 8, [metric("closure", "inner", 16)])],
    }

    assert violations(document) == [
        "crates/example.rs:12: inner cognitive complexity 16 exceeds 15"
    ]
