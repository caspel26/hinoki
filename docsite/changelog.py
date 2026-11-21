"""Render `CHANGELOG.md` as the explorable release notes page.

The page source holds only the `<!-- changelog -->` placeholder; this module
parses the release headings (`## [Unreleased]`, `## [0.1.0] - 2026-12-01`) and
returns HTML that `theme/static/js/changelog.js` turns into a version picker
and a section filter. Without JavaScript every release is shown.
"""

import html
import re
from dataclasses import dataclass

import markdown

PLACEHOLDER = "<!-- changelog -->"
RELEASE = re.compile(
    r"^##\s+\[(?P<name>[^\]]+)\](?:\s*-\s*(?P<date>\d{4}-\d{2}-\d{2}))?\s*$",
    re.MULTILINE,
)
SECTION = re.compile(r"^###\s+(?P<title>.+?)\s*$", re.MULTILINE)
RULE = re.compile(r"^---\s*$", re.MULTILINE)
KINDS = (
    ("features", "New Features"),
    ("improvements", "Improvements"),
    ("fixes", "Fixes"),
    ("docs", "Documentation"),
    ("summary", "Summary"),
)


@dataclass(frozen=True)
class Release:
    """One `## [name] - date` block of the changelog."""

    name: str
    date: str | None
    body: str

    @property
    def slug(self) -> str:
        return "v-" + re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")


def spans(matches: list[re.Match[str]], text: str) -> list[tuple[re.Match[str], int]]:
    """Each match with the offset where its block ends."""
    ends = [m.start() for m in matches[1:]] + [len(text)]
    return list(zip(matches, ends[: len(matches)], strict=True))


def parse(text: str) -> list[Release]:
    """Releases in file order, newest first."""
    return [
        Release(m.group("name"), m.group("date"), text[m.end() : end].strip())
        for m, end in spans(list(RELEASE.finditer(text)), text)
    ]


def kind_of(title: str) -> str:
    """Section type from its heading, ignoring the emoji."""
    lowered = title.lower()
    for kind, label in KINDS:
        if label.lower() in lowered:
            return kind
    return "other"


def render_markdown(source: str) -> str:
    return markdown.markdown(source, extensions=["extra", "sane_lists"])


def render_body(body: str) -> str:
    """Wrap each `###` section so the page can filter by type."""
    parts = []
    for m, end in spans(list(SECTION.finditer(body)), body):
        content = RULE.sub("", body[m.end() : end]).strip()
        parts.append(
            f'<section class="cl__sec" data-kind="{kind_of(m.group("title"))}">'
            f"<h3>{html.escape(m.group('title'))}</h3>"
            f"{render_markdown(content)}</section>"
        )
    return "".join(parts)


def render_card(release: Release, *, latest: bool, repo_url: str) -> str:
    """One release card with its tag, date and sections."""
    chip = ""
    if release.name.lower() == "unreleased":
        chip = '<span class="cl__chip cl__chip--next">Next</span>'
    elif latest:
        chip = '<span class="cl__chip">Latest</span>'
    date = f"<time>{release.date}</time>" if release.date else ""
    link = ""
    if release.date:
        tag = f"{repo_url}/releases/tag/v{html.escape(release.name)}"
        link = f'<a class="cl__link" href="{tag}">GitHub release</a>'
    return (
        f'<article class="cl__card" id="{release.slug}" data-version="{release.slug}">'
        f'<header class="cl__head"><h2>{html.escape(release.name)}</h2>'
        f"{chip}{date}{link}</header>"
        f"{render_body(release.body)}</article>"
    )


def render(text: str, repo_url: str) -> str:
    """The whole explorer: version picker, section filter and the cards."""
    releases = parse(text)
    if not releases:
        return '<p class="cl__empty">No releases are recorded yet.</p>'
    dated = [r for r in releases if r.date]
    latest = dated[0] if dated else None
    versions = "".join(
        f'<a class="cl__ver" href="#{r.slug}" data-target="{r.slug}">'
        f"{html.escape(r.name)}</a>"
        for r in releases
    )
    kinds = "".join(
        f'<button type="button" class="cl__kind" data-kind="{kind}" '
        f'aria-pressed="false">{label}</button>'
        for kind, label in KINDS
        if kind != "summary"
    )
    cards = "".join(
        render_card(r, latest=r is latest, repo_url=repo_url) for r in releases
    )
    return (
        '<div class="cl" data-changelog>'
        '<div class="cl__bar" data-search-exclude>'
        f'<nav class="cl__versions" aria-label="Versions">{versions}</nav>'
        f'<div class="cl__kinds" role="group" aria-label="Filter by type">{kinds}</div>'
        "</div>"
        f'<div class="cl__cards">{cards}</div></div>'
    )
