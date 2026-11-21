"""The changelog page generator behind `docs/changelog.md`."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "docsite_changelog", ROOT / "docsite" / "changelog.py"
)
assert _spec is not None and _spec.loader is not None
changelog = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = changelog
_spec.loader.exec_module(changelog)

SAMPLE = """# Notes

## [Unreleased]

### ✨ New Features

#### Thing
> `a.py` (#1)

Body.

## [0.1.0] - 2026-12-01

### 🐛 Fixes

- Fixed (#2).

---

### 🎯 Summary

Done.

## [0.0.9] - 2026-11-01

### 🔧 Improvements

- Better (#3).
"""


def test_parse_reads_names_dates_and_order() -> None:
    releases = changelog.parse(SAMPLE)
    assert [(r.name, r.date) for r in releases] == [
        ("Unreleased", None),
        ("0.1.0", "2026-12-01"),
        ("0.0.9", "2026-11-01"),
    ]
    assert releases[1].slug == "v-0-1-0"


def test_sections_are_typed_and_rules_dropped() -> None:
    body = changelog.render_body(changelog.parse(SAMPLE)[1].body)
    assert 'data-kind="fixes"' in body
    assert 'data-kind="summary"' in body
    assert "<hr" not in body


def test_latest_chip_marks_newest_dated_release_only() -> None:
    page = changelog.render(SAMPLE, "https://example.test/repo")
    assert page.count(">Latest<") == 1
    assert page.count(">Next<") == 1
    assert "https://example.test/repo/releases/tag/v0.1.0" in page
    assert page.index("v-0-1-0") < page.index('id="v-0-0-9"')


def test_headings_are_escaped_and_empty_file_is_explained() -> None:
    body = changelog.render_body("### <b>x</b>\n\ntext")
    assert "<b>x</b>" not in body
    assert "No releases" in changelog.render("# nothing", "https://example.test")
