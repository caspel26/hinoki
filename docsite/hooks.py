"""Page data for the Hinoki theme: reading time and resolved related pages."""

from mkdocs.config import Config
from mkdocs.structure.files import Files
from mkdocs.structure.pages import Page


def resolve_related(page: Page, files: Files) -> list[dict[str, str]]:
    """Resolve the curated `related:` front matter to titles and URLs."""
    found = []
    for name in (page.meta or {}).get("related", []):
        target = files.get_file_from_path(name)
        if target is not None and target.page is not None:
            found.append({"title": target.page.title, "url": target.page.url})
    return found


def on_page_markdown(markdown: str, page: Page, config: Config, files: Files) -> str:  # noqa: ARG001
    """Attach reading time and related pages to the page metadata."""
    page.meta["reading_minutes"] = max(1, round(len(markdown.split()) / 220))
    page.meta["related_pages"] = resolve_related(page, files)
    return markdown
