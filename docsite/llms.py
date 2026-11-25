"""Plain-text documentation for language models: `llms.txt`, `llms-full.txt` and
one Markdown mirror per page.

The module reads only strings and returns strings, so the build hook in
`hooks.py` does the file and MkDocs work and `tests/docsitellmstest.py` can
exercise it without MkDocs. Every problem is reported as a message; the hook
turns a non-empty list into a failed build.
"""

import inspect
import posixpath
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from urllib.parse import quote, unquote

INDEX_NAME = "llms.txt"
FULL_NAME = "llms-full.txt"
BLOCK_START = "<!-- llms:start -->"
BLOCK_END = "<!-- llms:end -->"
BLOCK_MAX_BYTES = 4096
DESCRIPTION_MAX = 160
SIGNATURE_MAX = 160
CHANGELOG = "changelog.md"
BLOCK_TITLE = "Write Hinoki, not FastAPI"
OPTIONAL_TITLE = "Optional"
INTRO_TAIL = (
    " Typed `Schema`, ORM and settings of its own."
    " Not built on FastAPI, Starlette, Pydantic or Django: their APIs do not"
    " exist here."
)

FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?\n)?---[ \t]*(?:\n|\Z)", re.DOTALL)
DESCRIPTION_KEY = re.compile(r"^description:[ \t]*(.+?)[ \t]*$", re.MULTILINE)
GUIDE_ENTRY = re.compile(r"^- \[[^\]]+\]\(([^)#\s]+\.md)\): (.+?)\s*$", re.MULTILINE)
# A backtick fence's info string cannot hold a backtick, or it is inline code.
FENCE = re.compile(r"^\s*(`{3,}(?=[^`\n]*\n?$)|~{3,})")
# A code span may wrap over a soft line break but not over a blank line.
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(?:(?!\n[ \t]*\n).)+?(?<!`)\1(?!`)", re.DOTALL)
INLINE_TARGET = re.compile(r"(\]\()(<[^>\n]*>|[^)\s]+)")
REFERENCE_DEFINITION = re.compile(
    r"^(\s{0,3}\[[^\]]+\]:[ \t]*)(<[^>\n]*>|\S+)", re.MULTILINE
)
TARGET_PARTS = re.compile(r"([^?#]*)(.*)", re.DOTALL)
FRONT_MATTER_VALUE = re.compile(r"^[\w-]+:[ \t]+(\S.*)$", re.MULTILINE)
ADDRESS = re.compile(r" at 0x[0-9a-fA-F]+")
LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
SCHEME = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|//)")
AUTODOC = re.compile(r"^:::[ \t]+(\S+)[ \t]*$")
HEADING_ONE = re.compile(r"^# (.+?)\s*$", re.MULTILINE)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z`\[(])")
SKIPPED_BLOCK = re.compile(r"^(?:#|!!!|\?\?\?|===|:::|\||<|>|-|\*|\+|\d+[.)]|\s{4})")

Resolver = Callable[[str], tuple[str, str]]


@dataclass(frozen=True)
class NavEntry:
    """A leaf of the navigation: a page, or an external link when `url` is set."""

    section: tuple[str, ...]
    title: str
    src_uri: str | None = None
    page_url: str = ""
    url: str | None = None


@dataclass(frozen=True)
class Policy:
    """The `extra.llms` settings of `mkdocs.yml`."""

    optional: tuple[str, ...]
    index_max_bytes: int
    full_max_bytes: int


@dataclass(frozen=True)
class Site:
    """Where the site is served and how it describes itself."""

    site_url: str
    description: str
    version: str
    repo_url: str
    policy: Policy


@dataclass(frozen=True)
class Inputs:
    """Everything read from MkDocs: the navigation, sources and file sets."""

    entries: tuple[NavEntry, ...]
    sources: Mapping[str, str]
    assets: frozenset[str]
    excluded: frozenset[str]
    resolver: Resolver


@dataclass
class Report:
    """The files to write, keyed by path under the site directory."""

    files: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def normalise_site_url(site_url: str) -> str:
    """The site URL with exactly one trailing slash."""
    return site_url.rstrip("/") + "/"


def version_label(site_url: str, fallback: str) -> str:
    """The version segment of a versioned site URL, else `fallback`."""
    path = site_url.split("://", 1)[-1].partition("/")[2].strip("/")
    return path.rsplit("/", 1)[-1] if path else fallback


def strip_front_matter(text: str) -> str:
    """The Markdown after a leading YAML front matter block."""
    return FRONT_MATTER.sub("", text, count=1)


def front_matter_description(text: str) -> str | None:
    """`description:` of the front matter, when the page sets one."""
    match = FRONT_MATTER.match(text)
    found = DESCRIPTION_KEY.search(match.group(1) or "") if match else None
    return found.group(1).strip("\"'") if found else None


def front_matter_problem(text: str) -> str | None:
    """Why the front matter is not valid YAML for MkDocs, if it is not."""
    match = FRONT_MATTER.match(text)
    for value in FRONT_MATTER_VALUE.findall(match.group(1) or "" if match else ""):
        plain = value[0] not in "\"'[{|>&*!"
        if (plain and (": " in value or value.endswith(":"))) or value[0] == "`":
            return f"front matter value needs quotes: {value[:60]}"
    return None


def flatten_links(text: str) -> str:
    """`[title](url)` reduced to `title`."""
    return LINK.sub(r"\1", text)


def guide_descriptions(guides: str) -> dict[str, str]:
    """Whole-page entries `- [Title](page.md): text` of the guides page."""
    found: dict[str, str] = {}
    for target, text in GUIDE_ENTRY.findall(guides):
        found.setdefault(target, flatten_links(text))
    return found


def split_fences(text: str) -> list[tuple[bool, str]]:
    """Split Markdown into `(is_fenced_code, chunk)` pieces, keeping newlines."""
    pieces: list[tuple[bool, str]] = []
    current: list[str] = []
    marker = ""
    for line in text.splitlines(keepends=True):
        opening = FENCE.match(line)
        if not marker and opening:
            pieces.append((False, "".join(current)))
            current, marker = [line], opening.group(1)
        elif marker and opening and closes(opening.group(1), marker, line):
            current.append(line)
            pieces.append((True, "".join(current)))
            current, marker = [], ""
        else:
            current.append(line)
    pieces.append((bool(marker), "".join(current)))
    return [piece for piece in pieces if piece[1]]


def closes(found: str, marker: str, line: str) -> bool:
    """Whether `line` ends the fence opened with `marker`."""
    return found[0] == marker[0] and len(found) >= len(marker) and line.strip() == found


def map_prose(text: str, change: Callable[[str], str]) -> str:
    """Apply `change` to everything outside fenced code and inline code."""
    out: list[str] = []
    for is_code, chunk in split_fences(text):
        out.append(chunk if is_code else map_inline(chunk, change))
    return "".join(out)


def map_inline(chunk: str, change: Callable[[str], str]) -> str:
    """Apply `change` to the parts of one prose chunk outside code spans."""
    out: list[str] = []
    last = 0
    for span in CODE_SPAN.finditer(chunk):
        out.append(change(chunk[last : span.start()]))
        out.append(span.group(0))
        last = span.end()
    out.append(change(chunk[last:]))
    return "".join(out)


def first_sentence(markdown: str) -> str | None:
    """The first sentence, at most 160 characters, of the first prose paragraph."""
    body = HEADING_ONE.split(strip_front_matter(markdown), maxsplit=1)[-1]
    for block in re.split(
        r"\n\s*\n", "".join(c for f, c in split_fences(body) if not f)
    ):
        text = block.strip("\n")
        if text.strip() and not SKIPPED_BLOCK.match(text):
            return shorten(flatten_links(" ".join(text.split())))
    return None


def shorten(paragraph: str) -> str:
    """Its first sentence, cut at a word boundary when too long."""
    sentence = SENTENCE_END.split(paragraph, maxsplit=1)[0]
    if len(sentence) <= DESCRIPTION_MAX:
        return sentence
    return sentence[: DESCRIPTION_MAX - 1].rsplit(" ", 1)[0] + "…"


def describe(entry: NavEntry, source: str, guides: Mapping[str, str]) -> str | None:
    """A page's description: front matter, then the guides page, then its text."""
    return (
        front_matter_description(source)
        or (guides.get(entry.src_uri or ""))
        or first_sentence(source)
    )


def extract_block(text: str, errors: list[str]) -> str:
    """The curated block between the `llms` markers."""
    start, end = text.find(BLOCK_START), text.find(BLOCK_END)
    if start < 0 or end < start:
        errors.append(f"the curated block needs {BLOCK_START} and {BLOCK_END}")
        return ""
    block = text[start + len(BLOCK_START) : end].strip()
    if len(block.encode()) > BLOCK_MAX_BYTES:
        errors.append(f"the curated block is over {BLOCK_MAX_BYTES} bytes")
    return block


def is_optional(entry: NavEntry, optional: Iterable[str]) -> bool:
    """Whether the policy puts this entry under `Optional`."""
    names = set(optional)
    return (
        entry.url is not None
        or entry.src_uri in names
        or any(title in names for title in entry.section)
    )


def unknown_optional(entries: Iterable[NavEntry], optional: Iterable[str]) -> list[str]:
    """Messages for policy entries that match no section title or page."""
    known: set[str | None] = set()
    for entry in entries:
        known.update(entry.section)
        known.add(entry.src_uri)
    return [
        f"extra.llms.optional matches nothing: {name}"
        for name in optional
        if name not in known
    ]


def heading_for(entry: NavEntry) -> str:
    """The `llms.txt` section a page belongs to."""
    if entry.section[0] == "Guides" and len(entry.section) > 1:
        return entry.section[1]
    return entry.section[0]


def optional_title(entry: NavEntry) -> str:
    """Optional titles carry their section, since they repeat across sections."""
    return f"{entry.section[-1]}: {entry.title}" if entry.section else entry.title


class Context:
    """Link resolution and error collection for one build."""

    def __init__(self, site: Site, inputs: Inputs, report: Report) -> None:
        self.site = site
        self.inputs = inputs
        self.report = report
        pages = [e for e in inputs.entries if e.src_uri]
        self.mirrored = {e.src_uri for e in pages if e.section}
        self.html_only = {e.src_uri: e.page_url for e in pages if not e.section}

    def resolve(self, raw: str, current: str) -> str:
        """The absolute URL for a link written relative to page `current`."""
        target = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
        if not target or SCHEME.match(target):
            return raw
        match = TARGET_PARTS.fullmatch(target)
        path, suffix = (match.group(1), match.group(2)) if match else (target, "")
        if not path:
            return f"{self.site.site_url}{current}{suffix}"
        found = self.locate(page_path(unquote(path), current), current, target)
        return found + suffix

    def locate(self, found: str, current: str, target: str) -> str:
        """The URL of the file `found`, or an error message when it has none."""
        base = self.site.site_url
        if found in self.inputs.excluded:
            self.report.errors.append(f"{current}: link to excluded page {target}")
        elif found in self.mirrored or found in self.inputs.assets:
            return base + quote(found)
        elif found in self.html_only:
            return base + self.html_only[found]
        else:
            self.report.errors.append(f"{current}: link to unknown file {target}")
        return target


def page_path(path: str, current: str) -> str:
    """A link path resolved against page `current`; a directory means its index."""
    if path.startswith("/"):
        joined = path.lstrip("/")
    else:
        joined = posixpath.join(posixpath.dirname(current), path)
    found = posixpath.normpath(joined)
    if path.endswith("/") or posixpath.basename(path) in {".", ".."}:
        return "index.md" if found == "." else posixpath.join(found, "index.md")
    return found


def rewrite_links(text: str, current: str, context: Context) -> str:
    """Make every relative link absolute, outside fenced and inline code."""

    def definition(match: re.Match[str]) -> str:
        return match.group(1) + context.resolve(match.group(2), current)

    def inline(match: re.Match[str]) -> str:
        return match.group(1) + context.resolve(match.group(2), current)

    def change(part: str) -> str:
        return INLINE_TARGET.sub(inline, part)

    out: list[str] = []
    for is_code, chunk in split_fences(text):
        if is_code:
            out.append(chunk)
        else:
            out.append(map_inline(REFERENCE_DEFINITION.sub(definition, chunk), change))
    return "".join(out)


def replace_autodoc(text: str, resolver: Resolver, link: Callable[[str], str]) -> str:
    """Replace each mkdocstrings block and its indented options by a bullet."""
    out: list[str] = []
    for is_code, chunk in split_fences(text):
        out.append(chunk if is_code else autodoc_chunk(chunk, resolver, link))
    return "".join(out)


def autodoc_chunk(chunk: str, resolver: Resolver, link: Callable[[str], str]) -> str:
    """`replace_autodoc` for one prose chunk."""
    out: list[str] = []
    skipping = False
    for line in chunk.splitlines(keepends=True):
        match = AUTODOC.match(line.rstrip("\n"))
        if match:
            display, summary = resolver(match.group(1))
            out.append(bullet(display, summary, link(match.group(1))) + "\n")
            skipping = True
        elif skipping and line[:1] in " \t" and line.strip():
            continue
        else:
            skipping = False
            out.append(line)
    return "".join(out)


def bullet(display: str, summary: str, url: str) -> str:
    """`- \\`name(sig)\\`: first docstring line. [reference](url)`."""
    summary = flatten_links(summary).strip().rstrip(".")
    tail = f": {summary}." if summary else "."
    return f"- `{display}`{tail} [reference]({url})"


MARKER_LINE = re.compile(r"^<!-- llms:(?:start|end) -->[ \t]*\n?", re.MULTILINE)
AI_PAGE = "ai-workflow.md"
GUIDES_PAGE = "guides.md"


def render_mirror(entry: NavEntry, context: Context) -> str:
    """One page as standalone Markdown with absolute links."""
    src_uri = entry.src_uri or ""
    body = strip_front_matter(context.inputs.sources[src_uri])
    if src_uri == CHANGELOG:
        title = HEADING_ONE.search(body)
        link = f"{context.site.repo_url}/blob/main/CHANGELOG.md"
        heading = title.group(1) if title else entry.title
        return f"# {heading}\n\nThe changelog is kept in [CHANGELOG.md]({link}).\n"
    base = context.site.site_url + entry.page_url
    body = MARKER_LINE.sub("", body)
    body = replace_autodoc(body, context.inputs.resolver, lambda path: f"{base}#{path}")
    return rewrite_links(body, src_uri, context).strip() + "\n"


def render_index(
    context: Context,
    descriptions: Mapping[str, str],
    block: str,
) -> str:
    """`llms.txt`: the curated block, then one line per page by section."""
    site = context.site
    policy = site.policy
    groups: dict[str, list[str]] = {}
    optional: list[str] = []
    for entry in navigable(context.inputs.entries):
        if is_optional(entry, policy.optional):
            optional.append(
                index_line(entry, descriptions, site, optional_title(entry))
            )
        else:
            line = index_line(entry, descriptions, site, entry.title)
            groups.setdefault(heading_for(entry), []).append(line)
    optional.append(
        f"- [Full documentation]({site.site_url}{FULL_NAME}):"
        " every non-optional page in one file"
    )
    head = (
        f"# Hinoki\n\n> {site.description}{INTRO_TAIL}\n\n"
        f"Docs version: {site.version}. Every page is Markdown at "
        f"{site.site_url}<page>.md; all non-optional pages are in "
        f"{site.site_url}{FULL_NAME}.\n\n## {BLOCK_TITLE}\n\n{block}\n"
    )
    sections = {**groups, OPTIONAL_TITLE: optional}
    body = "\n".join(
        f"## {name}\n\n" + "\n".join(lines) + "\n" for name, lines in sections.items()
    )
    return f"{head}\n{body}"


def navigable(entries: Iterable[NavEntry]) -> list[NavEntry]:
    """Entries that are listed: everything below a section, and external links."""
    return [entry for entry in entries if entry.section or entry.url]


def index_line(
    entry: NavEntry, descriptions: Mapping[str, str], site: Site, title: str
) -> str:
    """`- [title](url): description` for one entry."""
    if entry.url is not None:
        return f"- [{title}]({entry.url})"
    src_uri = entry.src_uri or ""
    return f"- [{title}]({site.site_url}{quote(src_uri)}): {descriptions[src_uri]}"


def render_full(context: Context, mirrors: Mapping[str, str]) -> str:
    """`llms-full.txt`: every non-optional page, titled and in navigation order."""
    parts: list[str] = []
    for entry in navigable(context.inputs.entries):
        if entry.src_uri and not is_optional(entry, context.site.policy.optional):
            body = HEADING_ONE.sub("", mirrors[entry.src_uri], count=1).strip()
            url = context.site.site_url + quote(entry.src_uri)
            parts.append(f"# {entry.title}\n\nSource: {url}\n\n{body}\n")
    return "\n".join(parts)


def link_targets(text: str) -> list[str]:
    """Link targets outside fenced and inline code."""
    found: list[str] = []

    def collect(part: str) -> str:
        found.extend(m.group(2) for m in INLINE_TARGET.finditer(part))
        return part

    for is_code, chunk in split_fences(text):
        if not is_code:
            found.extend(m.group(2) for m in REFERENCE_DEFINITION.finditer(chunk))
            map_inline(chunk, collect)
    return [t[1:-1] if t.startswith("<") and t.endswith(">") else t for t in found]


def link_problem(
    target: str, context: Context, valid: set[str], authored: str
) -> str | None:
    """Why `target` is not a usable link in an output file, if it is not."""
    base = context.site.site_url
    if not target.startswith(base):
        return None if SCHEME.match(target) else "relative link left in the output"
    path = unquote(re.split(r"[?#]", target[len(base) :], maxsplit=1)[0])
    if path in context.inputs.excluded:
        return "links to an excluded page"
    if path in valid or target in authored:
        return None
    return "does not resolve to a generated file or site asset"


def check_outputs(context: Context, files: Mapping[str, str]) -> list[str]:
    """Messages for outputs that are excluded or hold a link that breaks."""
    inputs = context.inputs
    valid = (
        set(files) | inputs.assets | {e.page_url for e in inputs.entries if e.src_uri}
    )
    authored = "\n".join(inputs.sources.values())
    errors = [
        f"{path}: output for an excluded page"
        for path in files
        if path in inputs.excluded
    ]
    errors += [
        f"{path}: holds a memory address"
        for path, text in files.items()
        if ADDRESS.search(text)
    ]
    for path, text in files.items():
        for target in link_targets(text):
            problem = link_problem(target, context, valid, authored)
            if problem:
                errors.append(f"{path}: {target} {problem}")
    return errors


def collect_descriptions(inputs: Inputs, errors: list[str]) -> dict[str, str]:
    """A description for every listed page, or an error naming the page."""
    guides = guide_descriptions(inputs.sources.get(GUIDES_PAGE, ""))
    found: dict[str, str] = {}
    for entry in navigable(inputs.entries):
        if entry.src_uri is None:
            continue
        text = describe(entry, inputs.sources[entry.src_uri], guides)
        if text:
            found[entry.src_uri] = text
        else:
            errors.append(
                f"{entry.src_uri}: no description; add `description:` front matter"
            )
    return found


def size_errors(site: Site, index: str, full: str) -> list[str]:
    """Messages for outputs over their byte caps."""
    errors = []
    if len(index.encode()) > site.policy.index_max_bytes:
        errors.append(f"{INDEX_NAME} is over {site.policy.index_max_bytes} bytes")
    if len(full.encode()) > site.policy.full_max_bytes:
        errors.append(f"{FULL_NAME} is over {site.policy.full_max_bytes} bytes")
    return errors


def build_outputs(site: Site, inputs: Inputs) -> Report:
    """Render every output and validate them; the report lists files and errors."""
    report = Report()
    context = Context(site, inputs, report)
    report.errors += unknown_optional(inputs.entries, site.policy.optional)
    for uri, text in inputs.sources.items():
        problem = front_matter_problem(text)
        if problem:
            report.errors.append(f"{uri}: {problem}")
    descriptions = collect_descriptions(inputs, report.errors)
    block = extract_block(inputs.sources.get(AI_PAGE, ""), report.errors)
    mirrors = {
        entry.src_uri: render_mirror(entry, context)
        for entry in navigable(inputs.entries)
        if entry.src_uri
    }
    if report.errors:
        return report
    index = render_index(context, descriptions, block)
    full = render_full(context, mirrors)
    report.files = {INDEX_NAME: index, FULL_NAME: full, **mirrors}
    report.errors += size_errors(site, index, full)
    report.errors += check_outputs(context, report.files)
    return report


def stable_default(value: object) -> object:
    """A default that prints the same in every process."""
    if inspect.isclass(value) or inspect.isroutine(value):
        return _Named(str(getattr(value, "__qualname__", "") or "callable"))
    return value


class _Named:
    """Prints as a name, whatever it replaces."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return self.name


def signature_text(target: object) -> str:
    """`inspect.signature` without process-specific reprs; may raise
    `ValueError` or `TypeError` for objects without a signature."""
    signature = inspect.signature(target)  # type: ignore[arg-type]
    params = [
        p if p.default is p.empty else p.replace(default=stable_default(p.default))
        for p in signature.parameters.values()
    ]
    text = str(signature.replace(parameters=params))
    text = ADDRESS.sub("", text)
    return "(...)" if len(text) > SIGNATURE_MAX else text


def autodoc_entry(path: str, target: object) -> tuple[str, str]:
    """Display name and docstring summary of a documented object.

    A missing signature keeps the summary and shows the bare name."""
    doc = inspect.getdoc(target) or ""
    summary = shorten(" ".join(doc.strip().split("\n\n")[0].split()))
    if not (inspect.isclass(target) or inspect.isroutine(target)):
        return path, summary
    try:
        return path + signature_text(target), summary
    except (ValueError, TypeError):
        return path, summary
