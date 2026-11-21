"""Draw the animated request-flow diagram of the README.

Run from the repository root; it needs only the standard library:

    uv run python scripts/readme_flow.py

It writes ``assets/readme/flow-dark.svg`` and ``flow-light.svg``. The steps come
from ``docs/request-lifecycle.md``; change them together. The animation is CSS
inside the SVG, so it plays when GitHub shows the file through ``<img>`` and
stands still under ``prefers-reduced-motion``.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

OUT = Path(__file__).resolve().parent.parent / "assets" / "readme"

WIDTH, HEIGHT = 992, 262
NODE_W, NODE_H, PITCH = 104, 76, 120
X0, NODE_Y = 24, 64
TRACK_Y = 168
CYCLE = 10.0  # seconds
TRAVEL = 8.0  # seconds the dot needs to cross the eight steps


class Step(NamedTuple):
    title: str
    detail: str


STEPS = (
    Step("Accept", "Kiso, Granian"),
    Step("Route", "match, bind"),
    Step("Validate", "input Schema"),
    Step("Access", "caller, policy"),
    Step("Begin", "atomic writes"),
    Step("Handler", "your function"),
    Step("Respond", "render"),
    Step("Commit", "or roll back"),
)

# (first step, last step, label): who runs the steps, from docs/request-lifecycle.md.
GROUPS = (
    (0, 0, "Server"),
    (1, 2, "Rust extension"),
    (3, 4, "Hinoki"),
    (5, 5, "Your code"),
    (6, 7, "Hinoki"),
)

FAILURE = (
    "On an error, an open transaction rolls back first; "
    "the error response carries a request ID."
)


class Palette(NamedTuple):
    panel: str
    panel_edge: str
    node: str
    node_edge: str
    lit: str
    lit_edge: str
    text: str
    muted: str
    accent: str
    warn: str


DARK = Palette(
    panel="#131317",
    panel_edge="#2c2c34",
    node="#1e1e24",
    node_edge="#3a3a44",
    lit="#2a2734",
    lit_edge="#b9a0e7",
    text="#eeedf2",
    muted="#9d9ca9",
    accent="#b9a0e7",
    warn="#e0bc7d",
)

LIGHT = Palette(
    panel="#ffffff",
    panel_edge="#ddd6cb",
    node="#f4f1ec",
    node_edge="#cdc5b8",
    lit="#efecf6",
    lit_edge="#5f3da5",
    text="#1d1a24",
    muted="#5f596a",
    accent="#5f3da5",
    warn="#8a5d16",
)

MONO = "'IBM Plex Mono', ui-monospace, 'SF Mono', Menlo, Consolas, monospace"


def center(index: int) -> float:
    return X0 + index * PITCH + NODE_W / 2


def style(p: Palette) -> str:
    step_delay = TRAVEL / (len(STEPS) - 1)
    end = TRAVEL / CYCLE * 100
    span = (len(STEPS) - 1) * PITCH
    delays = "\n".join(
        f".n{i} {{ animation-delay: {i * step_delay:.3f}s; }}"
        for i in range(len(STEPS))
    )
    return f"""
  text {{ font-family: {MONO}; }}
  .node {{ fill: {p.node}; stroke: {p.node_edge}; stroke-width: 1; }}
  .num {{ fill: {p.accent}; font-size: 12px; }}
  .title {{ fill: {p.text}; font-size: 15px; font-weight: 500; }}
  .detail {{ fill: {p.muted}; font-size: 12px; }}
  .group {{ fill: {p.muted}; font-size: 12px; letter-spacing: 0.06em;
    text-transform: uppercase; }}
  .rule {{ stroke: {p.node_edge}; stroke-width: 1; }}
  .track {{ stroke: {p.node_edge}; stroke-width: 2; }}
  .tick {{ fill: {p.node_edge}; }}
  .fail {{ fill: none; stroke: {p.warn}; stroke-width: 1; stroke-dasharray: 4 4;
    opacity: 0.7; }}
  .failtext {{ fill: {p.warn}; font-size: 12px; }}
  .dot {{ fill: {p.accent}; }}
  .halo {{ fill: {p.accent}; opacity: 0.25; }}
  .runner {{ opacity: 0; }}
  @keyframes lit {{
    0% {{ fill: {p.node}; stroke: {p.node_edge}; }}
    1%, 8% {{ fill: {p.lit}; stroke: {p.lit_edge}; }}
    10%, 100% {{ fill: {p.node}; stroke: {p.node_edge}; }}
  }}
  @keyframes travel {{
    0% {{ opacity: 0; transform: translateX(0); }}
    1% {{ opacity: 1; transform: translateX(0); }}
    {end:.0f}% {{ opacity: 1; transform: translateX({span}px); }}
    {end + 3:.0f}%, 100% {{ opacity: 0; transform: translateX({span}px); }}
  }}
  .node {{ animation: lit {CYCLE}s linear infinite; }}
  .runner {{ animation: travel {CYCLE}s linear infinite; animation-delay: 0.4s; }}
{delays}
  @media (prefers-reduced-motion: reduce) {{
    .node, .runner {{ animation: none; }}
    .runner {{ opacity: 0; }}
  }}
"""


def group_marks() -> str:
    out = []
    for first, last, label in GROUPS:
        left = X0 + first * PITCH
        right = X0 + last * PITCH + NODE_W
        out.append(f'<line class="rule" x1="{left}" y1="46" x2="{right}" y2="46"/>')
        out.append(
            f'<text class="group" x="{(left + right) / 2}" y="34" '
            f'text-anchor="middle">{label}</text>'
        )
    return "\n  ".join(out)


def label(cls: str, x: float, y: float, text: str) -> str:
    return f'<text class="{cls}" x="{x}" y="{y}" text-anchor="middle">{text}</text>'


def nodes() -> str:
    out = []
    for i, step in enumerate(STEPS):
        left = X0 + i * PITCH
        mid = center(i)
        out.append(
            f'<rect class="node n{i}" x="{left}" y="{NODE_Y}" width="{NODE_W}" '
            f'height="{NODE_H}" rx="12"/>'
            + label("num", mid, NODE_Y + 20, f"{i + 1:02d}")
            + label("title", mid, NODE_Y + 42, step.title)
            + label("detail", mid, NODE_Y + 62, step.detail)
        )
    return "\n  ".join(out)


def track() -> str:
    first, last = center(0), center(len(STEPS) - 1)
    ticks = "".join(
        f'<circle class="tick" cx="{center(i)}" cy="{TRACK_Y}" r="4"/>'
        for i in range(len(STEPS))
    )
    runner = (
        f'<g class="runner">'
        f'<circle class="halo" cx="{first}" cy="{TRACK_Y}" r="11"/>'
        f'<circle class="dot" cx="{first}" cy="{TRACK_Y}" r="6"/></g>'
    )
    line = f'x1="{first}" y1="{TRACK_Y}" x2="{last}" y2="{TRACK_Y}"'
    return f'<line class="track" {line}/>{ticks}{runner}'


def failure() -> str:
    left = X0 + 2 * PITCH
    right = X0 + 7 * PITCH + NODE_W
    top = 198
    return (
        f'<rect class="fail" x="{left}" y="{top}" width="{right - left}" '
        f'height="40" rx="10"/>'
        + label("failtext", (left + right) / 2, top + 24, FAILURE)
    )


def render(p: Palette) -> str:
    alt = (
        "Request flow: the server accepts the request, Hinoki routes and binds it, "
        "validates the input, checks access, opens a transaction, calls the handler, "
        "renders the response and commits. On an error an open transaction rolls "
        "back first, and the error response carries a request ID."
    )
    head = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'width="{WIDTH}" height="{HEIGHT}" role="img" aria-label="{alt}">'
    )
    panel = (
        f'<rect x="0.5" y="0.5" width="{WIDTH - 1}" height="{HEIGHT - 1}" rx="16" '
        f'fill="{p.panel}" stroke="{p.panel_edge}"/>'
    )
    body = "\n  ".join((panel, group_marks(), nodes(), track(), failure()))
    return f"{head}\n  <style>{style(p)}</style>\n  {body}\n</svg>\n"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, palette in (("dark", DARK), ("light", LIGHT)):
        path = OUT / f"flow-{name}.svg"
        path.write_text(render(palette), encoding="utf-8")
        print(f"wrote {path.relative_to(OUT.parent.parent)}")


if __name__ == "__main__":
    main()
