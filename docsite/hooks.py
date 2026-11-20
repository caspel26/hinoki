"""Page data for the Hinoki theme: reading time, related pages and the rule
catalogue behind the `HNK-…` popovers."""

import inspect
import re

from mkdocs.config import Config
from mkdocs.structure.files import Files
from mkdocs.structure.pages import Page

from hinoki import rules, waivers

RULE_NAMESPACES = (
    rules.Actions,
    rules.Transactions,
    rules.Schemas,
    rules.Models,
    rules.Imports,
)
WAIVER_NOTICES = {
    waivers.STALE: {
        "phase": "hinoki check · warning",
        "title": "A waiver matches no diagnostic.",
        "decl": waivers.NAME,
        "fix": "remove the waiver, or correct its rule or target",
    },
    waivers.EXPIRED: {
        "phase": "startup · warning",
        "title": "A waiver has expired and no longer applies.",
        "decl": waivers.NAME,
        "fix": "fix the violation, or renew the waiver with a new expires date",
    },
}
TAG = re.compile(r"<[^>]+>")
WORDS_PER_MINUTE = 220


def declaration(namespace: type, name: str, rule: type[rules.Rule]) -> str:
    """`Actions.require_access(match, except_)`, read from the constructor."""
    params = inspect.signature(rule).parameters.values()
    shown = ", ".join(
        f"*{p.name}" if p.kind is p.VAR_POSITIONAL else p.name for p in params
    )
    return f"{namespace.__name__}.{name}({shown})"


def rule_catalogue() -> dict[str, dict[str, str]]:
    """Every built-in rule and waiver notice, keyed by its `HNK-…` ID."""
    found: dict[str, dict[str, str]] = {}
    for namespace in RULE_NAMESPACES:
        for name, rule in vars(namespace).items():
            if isinstance(rule, type) and issubclass(rule, rules.Rule):
                found[rule.id] = {
                    "phase": rule.phase.value,
                    "title": rule.explanation,
                    "decl": declaration(namespace, name, rule),
                    "fix": rule.fix,
                }
    return found | WAIVER_NOTICES


def on_config(config: Config) -> Config:
    """Publish the rule catalogue to the theme."""
    config["extra"]["hn_rules"] = rule_catalogue()
    return config


def resolve_related(page: Page, files: Files) -> list[dict[str, str]]:
    """Resolve the curated `related:` front matter to titles and URLs."""
    found = []
    for name in (page.meta or {}).get("related", []):
        target = files.get_file_from_path(name)
        if target is not None and target.page is not None:
            found.append({"title": target.page.title, "url": target.page.url})
    return found


def on_page_markdown(markdown: str, page: Page, config: Config, files: Files) -> str:  # noqa: ARG001
    """Attach related pages to the page metadata."""
    page.meta["related_pages"] = resolve_related(page, files)
    return markdown


def on_page_content(html: str, page: Page, config: Config, files: Files) -> str:  # noqa: ARG001
    """Reading time from the rendered page, so generated API sections count."""
    words = len(TAG.sub(" ", html).split())
    page.meta["reading_minutes"] = max(1, round(words / WORDS_PER_MINUTE))
    return html
