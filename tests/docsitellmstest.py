"""The llms.txt generator behind the documentation site."""

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

import hinoki

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "docsite_llms", ROOT / "docsite" / "llms.py"
)
assert _spec is not None and _spec.loader is not None
llms = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = llms
_spec.loader.exec_module(llms)

BASE = "https://docs.example/dev/"
BLOCK_PAGE = (
    "---\ndescription: AI page.\n---\n# AI\n\n"
    f"{llms.BLOCK_START}\n* Use `Schema`.\n{llms.BLOCK_END}\n"
)


def entry(
    section: tuple[str, ...], title: str, src_uri: str | None = None, **kw: str
) -> Any:
    return llms.NavEntry(
        section,
        title,
        src_uri,
        page_url=(src_uri or "").removesuffix(".md") + "/",
        **kw,
    )


def fake_resolver(path: str) -> tuple[str, str]:
    return f"{path}(x)", "Does a thing."


def build(
    sources: dict[str, str],
    entries: list[Any],
    *,
    optional: tuple[str, ...] = (),
    excluded: frozenset[str] = frozenset(),
    assets: frozenset[str] = frozenset({"img/a.png"}),
    index_max: int = 24576,
    resolver: Any = None,
) -> Any:
    policy = llms.Policy(optional, index_max, 2097152)
    site = llms.Site(BASE, "Site.", "dev", "https://github.com/o/r", policy)
    sources = {"ai-workflow.md": BLOCK_PAGE, **sources}
    entries = [entry(("Guides",), "AI", "ai-workflow.md"), *entries]
    return llms.build_outputs(
        site,
        llms.Inputs(
            tuple(entries), sources, assets, excluded, resolver or fake_resolver
        ),
    )


def page(title: str, body: str = "Text here.") -> str:
    return f"# {title}\n\n{body}\n"


def test_nav_order_and_optional_last() -> None:
    report = build(
        {"a.md": page("A"), "b.md": page("B"), "r.md": page("R")},
        [
            entry(("Ref",), "R", "r.md"),
            entry(("Guides", "Data"), "A", "a.md"),
            entry(("Guides", "Data"), "B", "b.md"),
            entry(("Project",), "Roadmap", url="https://example.com/roadmap"),
        ],
        optional=("Ref", "Project"),
    )
    assert not report.errors
    index = report.files["llms.txt"]
    assert index.index("## Data") < index.index("## Optional")
    assert index.index("[A](") < index.index("[B](")
    optional = index.split("## Optional")[1]
    assert f"[Ref: R]({BASE}r.md)" in optional
    assert "[Project: Roadmap](https://example.com/roadmap)" in optional
    assert "r.md" not in report.files["llms-full.txt"]
    assert "r.md" in report.files


def test_unknown_optional_entry_is_an_error() -> None:
    report = build(
        {"a.md": page("A")}, [entry(("Guides",), "A", "a.md")], optional=("nope",)
    )
    assert any("nope" in message for message in report.errors)


def test_description_precedence() -> None:
    sources = {
        "guides.md": (
            "---\ndescription: G.\n---\n# G\n\n"
            "- [One](one.md): from guides.\n- [Two](two.md#x): no.\n"
        ),
        "one.md": "---\ndescription: from front matter\n---\n# One\n\nBody.\n",
        "two.md": page("Two", "First *words*. Second."),
    }
    entries = [
        entry(("Guides",), "G", "guides.md"),
        entry(("Guides", "D"), "One", "one.md"),
        entry(("Guides", "D"), "Two", "two.md"),
    ]
    index = build(sources, entries).files["llms.txt"]
    assert f"[One]({BASE}one.md): from front matter" in index
    assert f"[Two]({BASE}two.md): First *words*." in index
    sources["one.md"] = page("One")
    assert (
        f"[One]({BASE}one.md): from guides."
        in build(sources, entries).files["llms.txt"]
    )


def test_missing_description_is_an_error() -> None:
    report = build(
        {"a.md": "# A\n\n!!! note\n    only a note\n"},
        [entry(("Guides",), "A", "a.md")],
    )
    assert any("a.md: no description" in message for message in report.errors)


def test_links_are_rewritten_outside_code() -> None:
    body = (
        "[up](../c.md#h) [same](#top) [abs](/d/e.md) [img](/img/a.png) "
        "[ext](https://x.dev/a.md) `[code](c.md)`\n\n"
        "[ref]: ../c.md\n\n```md\n[fence](c.md)\n```\n"
    )
    report = build(
        {"d/e.md": page("E", body), "c.md": page("C")},
        [entry(("Guides",), "C", "c.md"), entry(("Guides",), "E", "d/e.md")],
    )
    assert not report.errors, report.errors
    text = report.files["d/e.md"]
    assert f"[up]({BASE}c.md#h)" in text
    assert f"[same]({BASE}d/e.md#top)" in text
    assert f"[abs]({BASE}d/e.md)" in text
    assert f"[img]({BASE}img/a.png)" in text
    assert "[ext](https://x.dev/a.md)" in text
    assert "`[code](c.md)`" in text
    assert "[fence](c.md)" in text
    assert f"[ref]: {BASE}c.md" in text


def test_asset_link_resolves_to_the_site_asset() -> None:
    report = build(
        {"a.md": page("A", "![x](img/a.png)")}, [entry(("Guides",), "A", "a.md")]
    )
    assert f"![x]({BASE}img/a.png)" in report.files["a.md"]


def test_links_to_excluded_and_unknown_pages_are_errors() -> None:
    entries = [entry(("Guides",), "A", "a.md")]
    sources = {"a.md": page("A", "[x](adr/one.md) [y](missing.md)")}
    report = build(sources, entries, excluded=frozenset({"adr/one.md"}))
    assert any("excluded page adr/one.md" in m for m in report.errors)
    assert any("unknown file missing.md" in m for m in report.errors)


def test_autodoc_block_is_replaced_by_a_bullet() -> None:
    body = "Intro.\n\n::: pkg.mod.Thing\n    options:\n      x: 1\n\nAfter.\n"
    report = build({"r.md": page("R", body)}, [entry(("Guides",), "R", "r.md")])
    text = report.files["r.md"]
    assert (
        f"- `pkg.mod.Thing(x)`: Does a thing. [reference]({BASE}r/#pkg.mod.Thing)"
        in text
    )
    assert "options:" not in text
    assert "\nAfter." in text


def test_front_matter_and_markers_are_stripped() -> None:
    text = build({}, []).files["ai-workflow.md"]
    assert not text.startswith("---")
    assert "llms:start" not in text
    assert llms.strip_front_matter("---\na: b\n---\nBody\n") == "Body\n"


def test_changelog_mirror_links_to_the_repository() -> None:
    report = build(
        {
            "changelog.md": (
                "---\ndescription: Log.\n---\n# Changelog\n\n<!-- changelog -->\n"
            )
        },
        [entry(("Guides",), "Changelog", "changelog.md")],
    )
    assert "github.com/o/r/blob/main/CHANGELOG.md" in report.files["changelog.md"]
    assert "<!--" not in report.files["changelog.md"]


def test_index_over_the_cap_is_an_error() -> None:
    report = build(
        {"a.md": page("A")}, [entry(("Guides",), "A", "a.md")], index_max=100
    )
    assert any("llms.txt is over 100 bytes" in m for m in report.errors)


@pytest.mark.parametrize(
    "text",
    [
        "# AI\n\nno markers\n",
        f"# AI\n\n{llms.BLOCK_START}\n{'x' * 5000}\n{llms.BLOCK_END}\n",
    ],
)
def test_the_curated_block_must_exist_and_fit(text: str) -> None:
    errors: list[str] = []
    llms.extract_block(text, errors)
    assert errors


def test_rendering_is_deterministic() -> None:
    args = (
        {"a.md": page("A", "[b](b.md)"), "b.md": page("B")},
        [
            entry(("Guides",), "A", "a.md"),
            entry(("Guides",), "B", "b.md"),
        ],
    )
    assert build(*args).files == build(*args).files


def test_version_label_follows_a_versioned_site_url() -> None:
    assert llms.version_label("https://h.dev/dev/", "0.0.1") == "dev"
    assert llms.version_label("https://h.dev/", "0.0.1") == "0.0.1"


def test_idiom_block_runs_and_the_absent_names_stay_absent() -> None:
    source = (ROOT / "docs" / "ai-workflow.md").read_text("utf-8")
    block = llms.extract_block(source, [])
    code = block.split("```python\n", 1)[1].split("```", 1)[0]
    namespace: dict[str, object] = {}
    exec(code, namespace)  # noqa: S102 - the documented example
    assert "/greeting" in namespace["app"].openapi()["paths"]  # type: ignore[attr-defined]
    for name in ("BaseModel", "Depends", "APIRouter"):
        assert not hasattr(hinoki, name)


def one_page(body: str, **kw: Any) -> Any:
    return build({"a.md": page("A", body)}, [entry(("Guides",), "A", "a.md")], **kw)


def test_front_matter_with_a_bare_colon_is_refused() -> None:
    bad = "---\ndescription: API for `Settings`: loading it.\n---\n# A\n"
    assert llms.front_matter_problem(bad)
    assert llms.front_matter_problem('---\ndescription: "A: b."\n---\n# A\n') is None
    report = build({"a.md": bad}, [entry(("Guides",), "A", "a.md")])
    assert any("a.md: front matter" in m for m in report.errors)


def test_every_docs_page_has_valid_front_matter() -> None:
    for path in sorted((ROOT / "docs").rglob("*.md")):
        problem = llms.front_matter_problem(path.read_text("utf-8"))
        assert problem is None, f"{path.name}: {problem}"


def clock() -> None: ...


SENTINEL = object()


def with_defaults(a: int = 1, now: Any = clock, obj: Any = SENTINEL) -> None: ...


def test_signatures_do_not_print_memory_addresses() -> None:
    text = llms.signature_text(with_defaults)
    assert "0x" not in text
    assert "now: 'Any' = clock" in text or "now: Any = clock" in text
    assert "(...)" not in text


def test_an_output_with_a_memory_address_is_an_error() -> None:
    def leaky(path: str) -> tuple[str, str]:
        return f"{path}(x=<object at 0x10ab>)", ""

    report = one_page("Intro.\n\n::: pkg.Thing", resolver=leaky)
    assert any("memory address" in m for m in report.errors)


class Unsigned:
    """Has a signature that cannot be read."""

    __signature__ = 5


@pytest.mark.parametrize("target", [max, Unsigned])
def test_a_missing_signature_keeps_the_docstring_summary(target: Any) -> None:
    display, summary = llms.autodoc_entry("pkg.name", target)
    assert display == "pkg.name"
    assert summary


def test_a_signature_is_added_when_there_is_one() -> None:
    display, summary = llms.autodoc_entry("pkg.f", with_defaults)
    assert display.startswith("pkg.f(a: int = 1") and summary == ""


def test_directory_angle_encoded_query_and_title_links() -> None:
    sources = {
        "index.md": "---\ntemplate: home.html\n---\n",
        "t/index.md": page("T"),
        "my page.md": page("M"),
        "a.md": page(
            "A",
            "[d](t/) [dot](./) [ang](<my page.md>) [enc](my%20page.md#x) "
            '[q](t/index.md?v=1#h) [ti](my%20page.md "Title")\n\n[r]: <t/index.md>',
        ),
    }
    entries = [
        llms.NavEntry((), "Home", "index.md", page_url=""),
        entry(("Guides",), "T", "t/index.md"),
        entry(("Guides",), "M", "my page.md"),
        entry(("Guides",), "A", "a.md"),
    ]
    report = build(sources, entries)
    assert not report.errors, report.errors
    text = report.files["a.md"]
    assert f"[d]({BASE}t/index.md)" in text
    assert f"[ang]({BASE}my%20page.md)" in text
    assert f"[enc]({BASE}my%20page.md#x)" in text
    assert f"[q]({BASE}t/index.md?v=1#h)" in text
    assert f'[ti]({BASE}my%20page.md "Title")' in text
    assert f"[r]: {BASE}t/index.md" in text
    assert f"[dot]({BASE})" in text


def test_reference_definition_text_after_inline_code_is_not_a_link() -> None:
    report = one_page("Use `code` [key]: value here.")
    assert not report.errors, report.errors


def test_inline_code_with_backticks_is_not_a_fence() -> None:
    body = "```x``` and [a](nowhere.md)\n\nMore."
    report = one_page(body)
    assert any("nowhere.md" in m for m in report.errors)


def test_code_spans_may_wrap_a_line() -> None:
    report = one_page("A `code\n[a](nowhere.md)` span.")
    assert not report.errors, report.errors
