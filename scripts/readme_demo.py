"""Render ``assets/readme/demo.gif`` from the real output of ``hinoki check``.

Run from the repository root with the locked environment::

    uv run python scripts/readme_demo.py

It scaffolds a throw-away project with ``hinoki new``, runs ``hinoki check`` on it,
adds an endpoint that breaks three rules, runs the check again and animates what the
terminal showed. Nothing in the GIF is typed by hand: re-run the script after a rule
or a message changes. It needs ``rsvg-convert`` and ``ffmpeg`` on the PATH.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "readme" / "demo.gif"
WIDTH, HEIGHT = 1080, 470
FONT = "JetBrainsMono Nerd Font Mono, JetBrains Mono, DejaVu Sans Mono, monospace"
SIZE, LEADING, PAD_X, PAD_TOP = 15, 22, 28, 70
COLORS = {
    "bg": "#211a2e",
    "bar": "#15111f",
    "line": "#35294a",
    "dot": "#4a3d66",
    "text": "#e8e1f2",
    "dim": "#8c80a3",
    "prompt": "#b48cf5",
    "bad": "#ff8a8a",
    "ok": "#7fe0a6",
    "fix": "#e3c29b",
    "loc": "#9db7ff",
}
TOKENS = [
    (re.compile(r"^\$ "), "prompt"),
    (re.compile(r"\[violated\]|exit 1"), "bad"),
    (re.compile(r"\b[0-9]+ violated\b"), "bad"),
    (re.compile(r"\b0 violated\b"), "ok"),
    (re.compile(r"^\s+fix:.*$"), "fix"),
    (re.compile(r"^[\w./-]+:\d+:"), "loc"),
    (re.compile(r"HNK-[A-Z]+-\d+"), "prompt"),
    (re.compile(r"^# .*$"), "dim"),
]
FRAME_MS = 60
TYPING_STEP = 3  # characters per frame while a command is typed
HOLD = {"after_command": 350, "after_output": 1800, "end": 3200}


@dataclass
class Step:
    """One thing the terminal does: type a command, or print lines."""

    kind: str  # "type" or "print"
    text: list[str]
    hold: int


def run(command: list[str], cwd: Path) -> str:
    """The merged stdout and stderr of a command, as a terminal would show it."""
    done = subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, check=False, env=env(cwd)
    )
    return (done.stdout + done.stderr).rstrip("\n")


def env(project: Path) -> dict[str, str]:
    import os  # noqa: PLC0415 - only the environment is needed here

    return {**os.environ, "PYTHONPATH": str(project), "NO_COLOR": "1"}


def transcript(workdir: Path) -> list[Step]:
    """Scaffold a project, check it, break it, check it again."""
    hinoki = [sys.executable, "-m", "hinoki"]
    created = run([*hinoki, "new", "shop", "--dir", str(workdir)], ROOT)
    project = workdir / "shop"
    target = [
        "shop.main:app",
        "--rules",
        "shop.rules:project_rules",
        "--package",
        "shop",
    ]
    clean = run([*hinoki, "check", *target], project)
    (project / "shop" / "api.py").write_text(
        (project / "shop" / "api.py").read_text()
        + "\n\nclass OrderIn(Schema):\n    item: str\n\n\n"
        + '@http.post("/orders", status=201)\n'
        + "def create_order(data: OrderIn) -> Health:\n"
        + '    return Health(status="created")\n'
    )
    routes = project / "shop" / "routes.py"
    routes.write_text(
        routes.read_text()
        .replace("import health", "import create_order, health")
        .replace("[health]", "[health, create_order]")
    )
    broken = run([*hinoki, "check", *target], project)
    check = "hinoki check shop.main:app --rules shop.rules:project_rules"
    return [
        Step("type", ["hinoki new shop"], HOLD["after_command"]),
        Step("print", created.replace(str(workdir) + "/", "").splitlines()[:1], 500),
        Step("type", [check], HOLD["after_command"]),
        Step("print", clean.splitlines(), HOLD["after_output"]),
        Step("type", ["# add POST /orders: no access, no atomic, unbounded text"], 500),
        Step("type", [check], HOLD["after_command"]),
        Step("print", [*broken.splitlines(), "exit 1"], HOLD["end"]),
    ]


def segments(line: str) -> list[tuple[str, str]]:
    """Split a line into ``(color, text)`` pieces using the token patterns."""
    colors = ["text"] * len(line)
    for pattern, color in TOKENS:
        for found in pattern.finditer(line):
            for index in range(found.start(), found.end()):
                colors[index] = color
    pieces: list[tuple[str, str]] = []
    for char, color in zip(line, colors, strict=True):
        if pieces and pieces[-1][0] == color:
            pieces[-1] = (color, pieces[-1][1] + char)
        else:
            pieces.append((color, char))
    return pieces


def svg(lines: list[str], cursor: bool) -> str:
    """One frame: the window chrome and the given terminal lines."""
    body = []
    visible = lines[-17:]
    for row, line in enumerate(visible):
        spans = "".join(
            f'<tspan fill="{COLORS[color]}">{escape(text)}</tspan>'
            for color, text in segments(line)
        )
        if cursor and row == len(visible) - 1:
            spans += f'<tspan fill="{COLORS["prompt"]}">▍</tspan>'
        y = PAD_TOP + row * LEADING
        body.append(f'<text x="{PAD_X}" y="{y}" xml:space="preserve">{spans}</text>')
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" font-family="{FONT}" font-size="{SIZE}">'
        f'<rect width="{WIDTH}" height="{HEIGHT}" rx="14" fill="{COLORS["bg"]}"/>'
        f'<path d="M0 14a14 14 0 0 1 14-14h{WIDTH - 28}a14 14 0 0 1 14 14v30H0z" '
        f'fill="{COLORS["bar"]}"/><rect y="44" width="{WIDTH}" height="1" '
        f'fill="{COLORS["line"]}"/>'
        + "".join(
            f'<circle cx="{26 + 20 * i}" cy="22" r="6" fill="{COLORS["dot"]}"/>'
            for i in range(3)
        )
        + f'<text x="{WIDTH / 2}" y="27" text-anchor="middle" fill="{COLORS["dim"]}" '
        'font-size="13">hinoki check</text>' + "".join(body) + "</svg>"
    )


def typed(step: Step, shown: list[str]) -> list[tuple[list[str], bool, int]]:
    """The frames of a command being typed, then held."""
    text = step.text[0]
    line = "" if text.startswith("#") else "$ "
    out = [
        ([*shown, line + text[:end]], True, FRAME_MS)
        for end in range(TYPING_STEP, len(text) + TYPING_STEP, TYPING_STEP)
    ]
    shown.append(line + text)
    out.append((list(shown), True, step.hold))
    return out


def printed(step: Step, shown: list[str]) -> list[tuple[list[str], bool, int]]:
    """The frames of output lines appearing one at a time, the last one held."""
    out = []
    for line in step.text:
        shown.append(line)
        out.append((list(shown), False, FRAME_MS * 2))
    out[-1] = (list(shown), False, step.hold)
    return out


def frames(steps: list[Step]) -> list[tuple[list[str], bool, int]]:
    """``(terminal lines, cursor, milliseconds)`` for every frame of the animation."""
    out: list[tuple[list[str], bool, int]] = []
    shown: list[str] = []
    for step in steps:
        out.extend((typed if step.kind == "type" else printed)(step, shown))
    return out


def render(items: list[tuple[list[str], bool, int]], workdir: Path) -> None:
    """Write PNG frames and join them into a GIF with ffmpeg's two-pass palette."""
    listing = []
    for number, (lines, cursor, ms) in enumerate(items):
        frame = workdir / f"f{number:04d}.png"
        subprocess.run(
            ["rsvg-convert", "-o", str(frame)],
            input=svg(lines, cursor).encode(),
            check=True,
        )
        listing.append(f"file '{frame}'\nduration {ms / 1000:.3f}")
    listing.append(f"file '{workdir / f'f{len(items) - 1:04d}.png'}'")
    (workdir / "frames.txt").write_text("\n".join(listing) + "\n")
    palette = workdir / "palette.png"
    base = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0"]
    source = [*base, "-i", str(workdir / "frames.txt")]
    scale = "scale=820:-1:flags=lanczos"
    subprocess.run(
        [*source, "-vf", f"{scale},palettegen=max_colors=48", str(palette)], check=True
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            *source,
            "-i",
            str(palette),
            "-lavfi",
            f"{scale}[x];[x][1:v]paletteuse",
            str(OUT),
        ],
        check=True,
    )


def main() -> None:
    for tool in ("rsvg-convert", "ffmpeg"):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} is needed to render the GIF")
    with tempfile.TemporaryDirectory(prefix="hinoki-demo-") as name:
        workdir = Path(name)
        items = frames(transcript(workdir))
        render(items, workdir)
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KiB)")


if __name__ == "__main__":
    main()
