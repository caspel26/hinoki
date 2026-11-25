"""Draw the animated architecture diagram of the README.

Run from the repository root; it needs only the standard library:

    uv run python scripts/readme_arch.py

It writes ``assets/readme/arch-dark.svg`` and ``arch-light.svg``: the "How the
parts fit together" diagram of the docsite home (``theme/home.html``), scaled
for the README. Keep the nodes in step with that diagram. The animation is CSS
inside the SVG (dots travel along the arrows), so it plays through ``<img>`` on
GitHub and stops under ``prefers-reduced-motion``.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from readme_flow import DARK, LIGHT, MONO, Palette

OUT = Path(__file__).resolve().parent.parent / "assets" / "readme"

PAD = 20
WIDTH, HEIGHT = 960 + 2 * PAD, 420 + 2 * PAD
SANS = "ui-sans-serif, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
DUR = 3.2  # seconds a dot needs to cross an arrow


class Node(NamedTuple):
    title: str
    sub: str
    kind: str
    x: int
    y: int
    w: int
    h: int


class Arrow(NamedTuple):
    path: str
    kind: str


NODES = (
    Node("Model", "tables and queries", "yours", 20, 40, 230, 52),
    Node("Schema", "accepts and returns", "yours", 20, 120, 230, 52),
    Node("Endpoint", "access=  atomic=  errors=", "yours", 20, 200, 230, 52),
    Node("One declared contract", "ordinary decorators", "core", 340, 98, 220, 104),
    Node("Routing", "", "rust", 670, 10, 270, 40),
    Node("Validation", "", "rust", 670, 62, 270, 40),
    Node("Access and transactions", "", "hinoki", 670, 114, 270, 40),
    Node("OpenAPI document", "", "server", 670, 166, 270, 40),
    Node("hinoki check rules", "", "amber", 670, 218, 270, 40),
    Node("Development MCP", "", "server", 670, 270, 270, 40),
    Node("Model", "", "yours", 20, 366, 120, 36),
    Node("migrations plan", "", "amber", 190, 366, 170, 36),
    Node("reviewed SQL file", "", "amber", 410, 366, 170, 36),
    Node("explicit apply", "", "amber", 630, 366, 150, 36),
    Node("Database", "", "server", 830, 366, 110, 36),
)

ARROWS = (
    Arrow("M250 66 C295 66 295 150 340 150", "yours"),
    Arrow("M250 146 C295 146 295 150 340 150", "yours"),
    Arrow("M250 226 C295 226 295 150 340 150", "yours"),
    Arrow("M560 150 C615 150 615 30 670 30", "rust"),
    Arrow("M560 150 C615 150 615 82 670 82", "rust"),
    Arrow("M560 150 C615 150 615 134 670 134", "hinoki"),
    Arrow("M560 150 C615 150 615 186 670 186", "server"),
    Arrow("M560 150 C615 150 615 238 670 238", "amber"),
    Arrow("M560 150 C615 150 615 290 670 290", "server"),
    Arrow("M140 384 L190 384", "amber"),
    Arrow("M360 384 L410 384", "amber"),
    Arrow("M580 384 L630 384", "amber"),
    Arrow("M780 384 L830 384", "amber"),
)

LANE_LABEL = "Table changes: explicit, never at startup"
ALT = (
    "Architecture: Model, Schema and Endpoint declarations form one declared "
    "contract, which routing, validation, access and transactions, the OpenAPI "
    "document, hinoki check rules and the development MCP all read. Table "
    "changes follow a separate lane: migrations plan, a reviewed SQL file, an "
    "explicit apply, then the database."
)

AMBER_TINT = {"dark": "#342c20", "light": "#f6eedf"}


def colours(p: Palette, theme: str) -> dict[str, tuple[str, str]]:
    """Return (edge, tint) for each kind of node."""
    kinds = {name: (k.edge, k.tint) for name, k in p.kinds.items()}
    kinds["amber"] = (p.warn, AMBER_TINT[theme])
    kinds["core"] = kinds["hinoki"]
    return kinds


def style(p: Palette, theme: str) -> str:
    rows = []
    for name, (edge, tint) in colours(p, theme).items():
        rows.append(f"  .k-{name} {{ --edge: {edge}; --tint: {tint}; }}")
    kinds = "\n".join(rows)
    return f"""
  text {{ font-family: {SANS}; }}
{kinds}
  .node {{ fill: var(--tint); stroke: var(--edge); stroke-width: 1; }}
  .core {{ stroke-width: 1.5; animation: breathe 4s ease-in-out infinite; }}
  .title {{ fill: {p.text}; font-size: 15px; font-weight: 600; }}
  .sub {{ fill: {p.muted}; font-size: 12px; font-family: {MONO}; }}
  .lane {{ fill: {p.warn}; font-size: 12.5px; font-family: {MONO}; }}
  .arrow {{ fill: none; stroke: var(--edge); stroke-width: 1.2; opacity: 0.85; }}
  .dot {{ fill: var(--edge); opacity: 0; offset-rotate: 0deg;
    animation: run {DUR}s cubic-bezier(0.4, 0, 0.2, 1) infinite; }}
  @keyframes run {{
    0% {{ offset-distance: 0%; opacity: 0; }}
    12% {{ opacity: 1; }}
    88% {{ opacity: 1; }}
    100% {{ offset-distance: 100%; opacity: 0; }}
  }}
  @keyframes breathe {{
    0%, 100% {{ filter: drop-shadow(0 0 0 transparent); }}
    50% {{ filter: drop-shadow(0 0 8px var(--edge)); }}
  }}
  @media (prefers-reduced-motion: reduce) {{
    .dot, .core {{ animation: none; }}
    .dot {{ opacity: 0; }}
  }}
"""


def markers(p: Palette, theme: str) -> str:
    out = []
    for name, (edge, _) in colours(p, theme).items():
        out.append(
            f'<marker id="a-{name}" viewBox="0 0 10 10" refX="9" refY="5" '
            f'markerWidth="7" markerHeight="7" orient="auto">'
            f'<path d="M0 0L10 5L0 10z" fill="{edge}"/></marker>'
        )
    return f"<defs>{''.join(out)}</defs>"


def arrows() -> str:
    out = []
    for i, arrow in enumerate(ARROWS):
        delay = (i * 0.37) % DUR
        out.append(
            f'<path class="arrow k-{arrow.kind}" d="{arrow.path}" '
            f'marker-end="url(#a-{arrow.kind})"/>'
            f'<circle class="dot k-{arrow.kind}" r="3" '
            f"style=\"offset-path: path('{arrow.path}'); "
            f'animation-delay: {delay:.2f}s"/>'
        )
    return "\n  ".join(out)


def node(n: Node) -> str:
    text_x, title_y = n.x + 14, n.y + n.h / 2 + 5
    if n.sub:
        title_y = n.y + n.h / 2 - (4 if n.kind == "core" else 8)
    cls = "node k-" + n.kind + (" core" if n.kind == "core" else "")
    out = (
        f'<rect class="{cls}" x="{n.x}" y="{n.y}" width="{n.w}" height="{n.h}" '
        f'rx="8"/><text class="title" x="{text_x}" y="{title_y}">{n.title}</text>'
    )
    if n.sub:
        out += f'<text class="sub" x="{text_x}" y="{title_y + 17}">{n.sub}</text>'
    return out


def render(p: Palette, theme: str) -> str:
    head = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'width="{WIDTH}" height="{HEIGHT}" role="img" aria-label="{ALT}">'
    )
    panel = (
        f'<rect x="0.5" y="0.5" width="{WIDTH - 1}" height="{HEIGHT - 1}" rx="16" '
        f'fill="{p.panel}" stroke="{p.panel_edge}"/>'
    )
    lane = f'<text class="lane" x="20" y="352">{LANE_LABEL}</text>'
    body = "\n  ".join(node(n) for n in NODES)
    return (
        f"{head}\n  <style>{style(p, theme)}</style>\n  {markers(p, theme)}\n"
        f'  {panel}\n  <g transform="translate({PAD} {PAD})">\n  {arrows()}\n'
        f"  {body}\n  {lane}\n  </g>\n</svg>\n"
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for theme, palette in (("dark", DARK), ("light", LIGHT)):
        path = OUT / f"arch-{theme}.svg"
        path.write_text(render(palette, theme), encoding="utf-8")
        print(f"wrote {path.relative_to(OUT.parent.parent)}")


if __name__ == "__main__":
    main()
