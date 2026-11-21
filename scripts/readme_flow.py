"""Draw the animated request-flow diagram of the README.

Run from the repository root; it needs only the standard library:

    uv run python scripts/readme_flow.py

It writes ``assets/readme/flow-dark.svg`` and ``flow-light.svg``. The steps come
from ``docs/request-lifecycle.md``; change them together. The animation is CSS
inside the SVG, so it plays when GitHub shows the file through ``<img>`` and
stands still under ``prefers-reduced-motion``. Colours are the docsite tokens in
``theme/static/css/tokens.css``: each owner of a step has its own colour.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

OUT = Path(__file__).resolve().parent.parent / "assets" / "readme"

WIDTH, HEIGHT = 992, 306
NODE_W, NODE_H, PITCH = 104, 76, 120
X0, NODE_Y = 24, 84
TRACK_Y, LANE_Y, BOX_Y = 194, 228, 246
CYCLE = 10.0  # seconds
START, DWELL, MOVE = 0.3, 0.35, 0.55  # seconds
ERROR_START, ERROR_END = 7.6, 9.2  # seconds


class Step(NamedTuple):
    title: str
    detail: str
    kind: str


STEPS = (
    Step("Accept", "Kiso, Granian", "server"),
    Step("Route", "match, bind", "rust"),
    Step("Validate", "input Schema", "rust"),
    Step("Access", "caller, policy", "hinoki"),
    Step("Begin", "atomic writes", "hinoki"),
    Step("Handler", "your function", "yours"),
    Step("Respond", "render", "hinoki"),
    Step("Commit", "or roll back", "hinoki"),
)

# (first step, last step, label, kind): who runs the steps, from
# docs/request-lifecycle.md.
GROUPS = (
    (0, 0, "Server", "server"),
    (1, 2, "Rust extension", "rust"),
    (3, 4, "Hinoki", "hinoki"),
    (5, 5, "Your code", "yours"),
    (6, 7, "Hinoki", "hinoki"),
)

FAILURE = (
    "On an error, an open transaction rolls back first; "
    "the error response carries a request ID."
)
SUBTITLE = "one request, start to finish"


class Kind(NamedTuple):
    edge: str
    tint: str


class Palette(NamedTuple):
    panel: str
    panel_edge: str
    node: str
    node_edge: str
    text: str
    muted: str
    brand: str
    wood: str
    warn: str
    kinds: dict[str, Kind]


DARK = Palette(
    panel="#131317",
    panel_edge="#2c2c34",
    node="#1e1e24",
    node_edge="#3a3a44",
    text="#eeedf2",
    muted="#9d9ca9",
    brand="#b9a0e7",
    wood="#e3c29b",
    warn="#e0bc7d",
    kinds={
        "server": Kind("#8fb8e0", "#252a34"),
        "rust": Kind("#e3c29b", "#2f2c2b"),
        "hinoki": Kind("#b9a0e7", "#2a2734"),
        "yours": Kind("#86cfb3", "#242d2e"),
    },
)

LIGHT = Palette(
    panel="#ffffff",
    panel_edge="#ddd6cb",
    node="#f4f1ec",
    node_edge="#cdc5b8",
    text="#1d1a24",
    muted="#5f596a",
    brand="#5f3da5",
    wood="#87522a",
    warn="#8a5d16",
    kinds={
        "server": Kind("#386b99", "#ebf0f5"),
        "rust": Kind("#87522a", "#f3eeea"),
        "hinoki": Kind("#5f3da5", "#efecf6"),
        "yours": Kind("#28745d", "#eaf1ef"),
    },
)

MONO = "'IBM Plex Mono', ui-monospace, 'SF Mono', Menlo, Consolas, monospace"


def center(index: int) -> float:
    return X0 + index * PITCH + NODE_W / 2


def arrival(index: int) -> float:
    return START + index * (DWELL + MOVE)


def pct(seconds: float) -> float:
    return seconds / CYCLE * 100


def travel_keyframes() -> str:
    ease = "animation-timing-function: ease-in-out;"
    rows = ["0% { opacity: 0; transform: translateX(0); }"]
    stops = [
        (t, i * PITCH)
        for i in range(len(STEPS))
        for t in (arrival(i), arrival(i) + DWELL)
    ]
    rows.extend(
        f"{pct(t):.2f}% {{ opacity: 1; transform: translateX({x}px); {ease} }}"
        for t, x in stops
    )
    last = (len(STEPS) - 1) * PITCH
    rows.append(f"{pct(7.5):.2f}% {{ opacity: 0; transform: translateX({last}px); }}")
    rows.append("100% { opacity: 0; transform: translateX(0); }")
    return "\n    ".join(rows)


def error_keyframes() -> str:
    span = 3 * PITCH
    return "\n    ".join(
        (
            f"0%, {pct(ERROR_START) - 0.5:.2f}% {{ opacity: 0; "
            "transform: translateX(0); }",
            f"{pct(ERROR_START):.2f}% {{ opacity: 1; transform: translateX(0); "
            "animation-timing-function: ease-in-out; }",
            f"{pct(ERROR_END):.2f}% {{ opacity: 1; "
            f"transform: translateX(-{span}px); }}",
            f"{pct(ERROR_END) + 2:.2f}%, 100% {{ opacity: 0; "
            f"transform: translateX(-{span}px); }}",
        )
    )


def style(p: Palette) -> str:
    kinds = "\n".join(
        f"  .k-{name} {{ --edge: {k.edge}; --tint: {k.tint}; }}"
        for name, k in p.kinds.items()
    )
    delays = "\n".join(
        f"  .n{i} {{ animation-delay: {arrival(i):.2f}s; }}" for i in range(len(STEPS))
    )
    return f"""
  text {{ font-family: {MONO}; }}
{kinds}
  .node {{ fill: {p.node}; stroke: {p.node_edge}; stroke-width: 1; }}
  .k-yours.node {{ stroke-dasharray: 5 4; }}
  .num {{ fill: var(--edge); font-size: 12px; font-weight: 600; }}
  .title {{ fill: {p.text}; font-size: 15px; font-weight: 500; }}
  .detail {{ fill: {p.muted}; font-size: 12px; }}
  .group {{ fill: var(--edge); font-size: 12px; letter-spacing: 0.06em;
    text-transform: uppercase; }}
  .rule {{ stroke: var(--edge); stroke-width: 2; stroke-linecap: round;
    opacity: 0.85; }}
  .mark {{ fill: {p.brand}; font-size: 17px; font-weight: 600;
    letter-spacing: 0.02em; }}
  .sub {{ fill: {p.muted}; font-size: 12px; }}
  .track {{ stroke: {p.node_edge}; stroke-width: 2; }}
  .tick {{ fill: {p.node_edge}; }}
  .lane {{ stroke: {p.warn}; stroke-width: 1.5; stroke-dasharray: 4 4;
    opacity: 0.8; }}
  .arrow {{ fill: {p.warn}; }}
  .fail {{ fill: none; stroke: {p.warn}; stroke-width: 1; stroke-dasharray: 4 4;
    opacity: 0.7; }}
  .failtext {{ fill: {p.warn}; font-size: 12px; }}
  .dot {{ fill: {p.brand}; }}
  .halo {{ fill: {p.brand}; opacity: 0.25; }}
  .edot {{ fill: {p.warn}; }}
  .ehalo {{ fill: {p.warn}; opacity: 0.25; }}
  .runner, .err {{ opacity: 0; }}
  @keyframes lit {{
    0%, 5% {{ fill: var(--tint); stroke: var(--edge); }}
    12%, 100% {{ fill: {p.node}; stroke: {p.node_edge}; }}
  }}
  @keyframes travel {{
    {travel_keyframes()}
  }}
  @keyframes fail {{
    {error_keyframes()}
  }}
  .node {{ animation: lit {CYCLE}s linear infinite; }}
  .runner {{ animation: travel {CYCLE}s linear infinite; }}
  .err {{ animation: fail {CYCLE}s linear infinite; }}
{delays}
  @media (prefers-reduced-motion: reduce) {{
    .node, .runner, .err {{ animation: none; }}
    .runner, .err {{ opacity: 0; }}
  }}
"""


def label(cls: str, x: float, y: float, text: str, anchor: str = "middle") -> str:
    return f'<text class="{cls}" x="{x}" y="{y}" text-anchor="{anchor}">{text}</text>'


def header() -> str:
    return "".join(
        (
            label("mark", 28, 36, "hinoki", "start"),
            label("sub", WIDTH - 28, 35, SUBTITLE, "end"),
        )
    )


def group_marks() -> str:
    out = []
    for first, last, name, kind in GROUPS:
        left = X0 + first * PITCH
        right = X0 + last * PITCH + NODE_W
        out.append(
            f'<line class="rule k-{kind}" x1="{left}" y1="72" x2="{right}" y2="72"/>'
        )
        out.append(label(f"group k-{kind}", (left + right) / 2, 62, name))
    return "\n  ".join(out)


def nodes() -> str:
    out = []
    for i, step in enumerate(STEPS):
        left = X0 + i * PITCH
        mid = center(i)
        out.append(
            f'<rect class="node k-{step.kind} n{i}" x="{left}" y="{NODE_Y}" '
            f'width="{NODE_W}" height="{NODE_H}" rx="12"/>'
            + label(f"num k-{step.kind}", mid, NODE_Y + 20, f"{i + 1:02d}")
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
        '<g class="runner">'
        f'<circle class="halo" cx="{first}" cy="{TRACK_Y}" r="11"/>'
        f'<circle class="dot" cx="{first}" cy="{TRACK_Y}" r="6"/></g>'
    )
    line = f'x1="{first}" y1="{TRACK_Y}" x2="{last}" y2="{TRACK_Y}"'
    return f'<line class="track" {line}/>{ticks}{runner}'


def error_lane() -> str:
    start, end = center(5), center(2)
    drop = (
        f'<line class="lane" x1="{start}" y1="{TRACK_Y + 8}" '
        f'x2="{start}" y2="{LANE_Y}"/>'
    )
    lane = (
        f'<line class="lane" x1="{start}" y1="{LANE_Y}" x2="{end + 6}" y2="{LANE_Y}"/>'
    )
    head = (
        f'<polygon class="arrow" points="{end},{LANE_Y} {end + 8},{LANE_Y - 5} '
        f'{end + 8},{LANE_Y + 5}"/>'
    )
    pulse = (
        '<g class="err">'
        f'<circle class="ehalo" cx="{start}" cy="{LANE_Y}" r="9"/>'
        f'<circle class="edot" cx="{start}" cy="{LANE_Y}" r="5"/></g>'
    )
    return drop + lane + head + pulse


def failure() -> str:
    left = X0 + 2 * PITCH
    right = X0 + 7 * PITCH + NODE_W
    return (
        f'<rect class="fail" x="{left}" y="{BOX_Y}" width="{right - left}" '
        f'height="40" rx="10"/>'
        + label("failtext", (left + right) / 2, BOX_Y + 24, FAILURE)
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
    parts = (panel, header(), group_marks(), nodes(), track(), error_lane(), failure())
    body = "\n  ".join(parts)
    return f"{head}\n  <style>{style(p)}</style>\n  {body}\n</svg>\n"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, palette in (("dark", DARK), ("light", LIGHT)):
        path = OUT / f"flow-{name}.svg"
        path.write_text(render(palette), encoding="utf-8")
        print(f"wrote {path.relative_to(OUT.parent.parent)}")


if __name__ == "__main__":
    main()
