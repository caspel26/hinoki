"""Page data for the Hinoki theme: reading time, related pages and the rule
catalogue behind the `HNK-…` popovers."""

import importlib
import importlib.util
import inspect
import logging
import re
import sys
import tomllib
from pathlib import Path
from types import ModuleType

from mkdocs.config import Config
from mkdocs.exceptions import PluginError
from mkdocs.structure.files import Files
from mkdocs.structure.nav import Link, Navigation, Section
from mkdocs.structure.pages import Page
from mkdocs.utils.meta import get_data

from hinoki import rules, waivers


def load_sibling(name: str) -> ModuleType:
    """Load `docsite/<name>.py` by path, as mkdocs loads this file."""
    spec = importlib.util.spec_from_file_location(
        f"docsite_{name}", Path(__file__).with_name(f"{name}.py")
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load docsite/{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


changelog = load_sibling("changelog")
llms = load_sibling("llms")
log = logging.getLogger("mkdocs.hooks.llms")
# Filled by on_nav for the build in progress and read by on_post_build.
build_state: dict[str, object] = {}

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


def entry(rule: type[rules.Rule], decl: str) -> dict[str, str]:
    return {
        "phase": rule.phase.value,
        "title": rule.explanation,
        "decl": decl,
        "fix": rule.fix,
    }


def rule_catalogue() -> dict[str, dict[str, str]]:
    """Every built-in rule and waiver notice, keyed by its `HNK-…` ID."""
    found: dict[str, dict[str, str]] = {}
    for namespace in RULE_NAMESPACES:
        for name, rule in vars(namespace).items():
            if isinstance(rule, type) and issubclass(rule, rules.Rule):
                found[rule.id] = entry(rule, declaration(namespace, name, rule))
    # A rule exported on its own, not through a namespace.
    found[rules.EmailTemplatesMatch.id] = entry(
        rules.EmailTemplatesMatch, "EmailTemplatesMatch()"
    )
    return found | WAIVER_NOTICES


def project_version(config: Config) -> str:
    """`project.version` of the pyproject.toml beside the docs."""
    path = Path(config["docs_dir"]).parent / "pyproject.toml"
    version: str = tomllib.loads(path.read_text("utf-8"))["project"]["version"]
    return version


def on_config(config: Config) -> Config:
    """Publish the rule catalogue and the released version to the theme."""
    config["extra"]["hn_rules"] = rule_catalogue()
    config["extra"]["hn_version"] = project_version(config)
    return config


def resolve_related(page: Page, files: Files) -> list[dict[str, str]]:
    """Resolve the curated `related:` front matter to titles and URLs."""
    found = []
    for name in (page.meta or {}).get("related", []):
        target = files.get_file_from_path(name)
        if target is not None and target.page is not None:
            found.append({"title": target.page.title, "url": target.page.url})
    return found


def on_page_markdown(markdown: str, page: Page, config: Config, files: Files) -> str:
    """Attach related pages to the page metadata."""
    page.meta["related_pages"] = resolve_related(page, files)
    if changelog.PLACEHOLDER in markdown:
        source = (Path(config["docs_dir"]).parent / "CHANGELOG.md").read_text("utf-8")
        page.meta["changelog"] = True
        return markdown.replace(
            changelog.PLACEHOLDER, changelog.render(source, config["repo_url"])
        )
    return markdown


def on_page_content(html: str, page: Page, config: Config, files: Files) -> str:  # noqa: ARG001
    """Reading time from the rendered page, so generated API sections count."""
    words = len(TAG.sub(" ", html).split())
    page.meta["reading_minutes"] = max(1, round(words / WORDS_PER_MINUTE))
    return html


def flatten_nav(
    items: list[object], section: tuple[str, ...] = ()
) -> list["llms.NavEntry"]:
    """Navigation leaves in order; excluded pages are left out."""
    found: list[llms.NavEntry] = []
    for item in items:
        if isinstance(item, Section):
            found += flatten_nav(item.children, (*section, item.title))
        elif isinstance(item, Link):
            found.append(llms.NavEntry(section, item.title, url=item.url))
        elif isinstance(item, Page) and item.file.inclusion.is_included():
            found.append(
                llms.NavEntry(
                    section,
                    item.title or item.file.src_uri,
                    item.file.src_uri,
                    item.url,
                )
            )
    return found


def on_nav(nav: Navigation, config: Config, files: Files) -> Navigation:  # noqa: ARG001
    """Record the navigation and the file sets for the llms.txt outputs."""
    entries = flatten_nav(list(nav.items))
    by_uri = {file.src_uri: file for file in files}
    build_state.clear()
    build_state["entries"] = tuple(entries)
    build_state["sources"] = {
        entry.src_uri: by_uri[entry.src_uri].abs_src_path
        for entry in entries
        if entry.src_uri
    }
    build_state["excluded"] = frozenset(
        file.src_uri for file in files if not file.inclusion.is_included()
    )
    build_state["assets"] = frozenset(
        file.src_uri
        for file in files
        if not file.is_documentation_page() and file.inclusion.is_included()
    )
    return nav


def import_object(path: str) -> object:
    """The module, class or function a dotted path names."""
    parts = path.split(".")
    for end in range(len(parts), 0, -1):
        try:
            found: object = importlib.import_module(".".join(parts[:end]))
        except ImportError:
            continue
        for name in parts[end:]:
            found = getattr(found, name)
        return found
    raise ImportError(path)


def autodoc_summary(path: str) -> tuple[str, str]:
    """Signature and first docstring paragraph of a documented object."""
    try:
        target = import_object(path)
    except Exception:
        return path, ""
    return llms.autodoc_entry(path, target)


def llms_inputs() -> "llms.Inputs":
    """The recorded navigation with sources read from disk."""
    sources = {
        uri: Path(str(path)).read_text("utf-8")
        for uri, path in build_state["sources"].items()  # type: ignore[attr-defined]
    }
    for uri, text in sources.items():
        if text.startswith("---") and not get_data(text)[1]:
            raise PluginError(f"{uri}: front matter is not valid YAML")
    return llms.Inputs(
        entries=build_state["entries"],  # type: ignore[arg-type]
        sources=sources,
        assets=build_state["assets"],  # type: ignore[arg-type]
        excluded=build_state["excluded"],  # type: ignore[arg-type]
        resolver=autodoc_summary,
    )


def llms_site(config: Config) -> "llms.Site":
    """The site settings, with the version-aware URL mike sets."""
    settings = config["extra"].get("llms", {})
    site_url = llms.normalise_site_url(config["site_url"])
    policy = llms.Policy(
        optional=tuple(settings.get("optional", ())),
        index_max_bytes=int(settings.get("index_max_bytes", 24576)),
        full_max_bytes=int(settings.get("full_max_bytes", 2097152)),
    )
    return llms.Site(
        site_url=site_url,
        description=config["site_description"],
        version=llms.version_label(site_url, config["extra"]["hn_version"]),
        repo_url=config["repo_url"],
        policy=policy,
    )


def on_post_build(config: Config) -> None:
    """Write llms.txt, llms-full.txt and the Markdown mirrors into the site."""
    report = llms.build_outputs(llms_site(config), llms_inputs())
    if report.errors:
        raise PluginError("llms.txt: " + "; ".join(report.errors))
    root = Path(config["site_dir"])
    for name, text in report.files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, "utf-8")
    for name in (llms.INDEX_NAME, llms.FULL_NAME):
        size = len(report.files[name].encode())
        log.info("%s: %d bytes, about %d tokens", name, size, size // 4)
