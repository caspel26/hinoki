"""Small terminal styling for the interactive commands (ANSI, standard library only).

Colours are the site palette (``theme/static/css/tokens.css``, dark theme). Colour
and Unicode symbols are used only on a capable terminal; piped output,
``NO_COLOR`` and ``TERM=dumb`` get plain ASCII text with no escape sequences.
Ordinary text keeps the terminal's own foreground so light themes stay readable;
the palette is used for accents.
"""

import os
import re
import shutil
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

RESET = "\x1b[0m"
CLEAR_LINE = "\x1b[2K"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
RIGHT_ANGLE = "\u203a"
POINTER = "\u276f"
MIDDLE_DOT = "\u00b7"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# name: (dark-theme hex, light-theme hex, nearest basic ANSI foreground code),
# from the site tokens: text, text-2/3, brand, mark light/shade, wood, info and
# the semantic colours.
PALETTE: dict[str, tuple[str, str, int]] = {
    "text": ("#eeedf2", "#1d1a24", 39),
    "soft": ("#cfcdd8", "#4d4859", 37),
    "dim": ("#9d9ca9", "#5f596a", 90),
    "brand": ("#b9a0e7", "#5f3da5", 35),
    "light": ("#cdaefc", "#9a7be0", 95),
    "dot": ("#a98bf0", "#5f3da5", 35),
    "shade": ("#7a55c4", "#5f3da5", 35),
    "wood": ("#e3c29b", "#87522a", 33),
    "info": ("#8fb8e0", "#386b99", 34),
    "good": ("#86cfb3", "#28745d", 32),
    "warn": ("#e0bc7d", "#8a5d16", 33),
    "bad": ("#ff7a8a", "#b44058", 31),
}

# kind: (unicode glyph, ASCII glyph, colour) for lines on the guide rail
GLYPHS: dict[str, tuple[str, str, str]] = {
    "rail": ("\u2502", "|", "shade"),
    "start": ("\u250c", "+", "shade"),
    "end": ("\u2514", "+", "shade"),
    "active": ("\u25c6", "*", "brand"),
    "done": ("\u25c7", "o", "good"),
    "ok": ("\u2714", "+", "good"),
    "skip": ("\u2013", "-", "warn"),
    "warn": ("\u25b2", "!", "warn"),
    "unsure": ("?", "?", "bad"),
    "bad": ("\u2718", "x", "bad"),
    "cancel": ("\u25a0", "x", "bad"),
}

CUBE = (0, 95, 135, 175, 215, 255)

# Draws the banner's tagline row, with the glint at a position or without it.
Row = Callable[[int | None], str]


def rgb(hex_color: str) -> tuple[int, int, int]:
    return (
        int(hex_color[1:3], 16),
        int(hex_color[3:5], 16),
        int(hex_color[5:7], 16),
    )


def cube_index(value: int) -> int:
    return min(range(len(CUBE)), key=lambda index: abs(CUBE[index] - value))


def xterm256(red: int, green: int, blue: int) -> int:
    """The nearest colour in the 6x6x6 cube of the 256-colour palette."""
    return 16 + 36 * cube_index(red) + 6 * cube_index(green) + cube_index(blue)


def visible_width(text: str) -> int:
    return len(ANSI.sub("", text))


def clip_start(word: str, room: int, ellipsis: str) -> str:
    """``word`` cut to ``room`` columns, losing its start."""
    if len(word) <= room:
        return word
    return ellipsis + word[len(word) - room + len(ellipsis) :]


def wrap_value(value: str, room: int, ellipsis: str = "\u2026") -> list[str]:
    """``value`` broken at spaces into rows of at most ``room`` columns; one
    word that is too long loses its start."""
    rows = [""]
    for text in value.split(" "):
        word = clip_start(text, room, ellipsis)
        joined = f"{rows[-1]} {word}".strip()
        if len(joined) <= room or not rows[-1]:
            rows[-1] = joined
        else:
            rows.append(word)
    return rows


def fit(text: str, width: int) -> str:
    """``text`` cut to ``width`` visible columns, keeping its escape sequences."""
    if visible_width(text) <= width:
        return text
    kept: list[str] = []
    count = 0
    for token in re.findall(r"\x1b\[[0-9;?]*[A-Za-z]|.", text):
        if token.startswith("\x1b"):
            kept.append(token)
        elif count < width - 1:
            kept.append(token)
            count += 1
    return "".join(kept) + "\u2026" + RESET


def elapsed(seconds: float) -> str:
    if seconds < 1:
        return f"{round(seconds * 1000)}ms"
    return f"{seconds:.1f}s"


def shown_path(target: Path) -> str:
    """``target`` relative to here when inside it, else with ``~`` for home
    (as written or with its symlinks resolved)."""
    try:
        return str(target.relative_to(Path.cwd()))
    except ValueError:
        pass
    text = str(target)
    home = Path.home()
    for prefix in dict.fromkeys((str(home), str(home.resolve()))):
        if text.startswith(prefix + os.sep):
            return "~" + text[len(prefix) :]
    return text


SPINNER = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"
SPINNER_COLORS = ("shade", "brand", "light", "brand")
TAGLINE = "Python API framework"
GLINT_FADE = 3
LABEL_WIDTH = 10
# The banner branch: its top, its side and its bottom, in Unicode and ASCII, and
# the widest and narrowest value column of its facts.
BRANCH_UNICODE = ("\u256d\u2500", "\u2502", "\u2570\u2500")
BRANCH_ASCII = ("+-", "|", "+-")
FACTS_MAX = 72
FACTS_MIN = 20


def light_background(env: Mapping[str, str]) -> bool:
    """Whether ``COLORFGBG`` (set by some terminals) names a light background."""
    return env.get("COLORFGBG", "").rpartition(";")[2] in {"7", "15"}


def color_depth(env: Mapping[str, str]) -> str:
    if env.get("COLORTERM", "").lower() in {"truecolor", "24bit"}:
        return "truecolor"
    return "256" if "256" in env.get("TERM", "") else "16"


class Style:
    """Paints text and chooses symbols for one output stream.

    ``depth`` is ``"truecolor"``, ``"256"`` or ``"16"``; ``width`` is the
    terminal's width in columns.
    """

    def __init__(
        self,
        *,
        color: bool = False,
        unicode: bool = False,
        depth: str = "16",
        animated: bool = False,
        light: bool = False,
        width: int = 80,
    ) -> None:
        self.color = color
        self.unicode = unicode
        self.depth = depth
        self.animated = animated
        self.light = light
        self.width = width

    @classmethod
    def detect(
        cls, stream: TextIO | None = None, env: Mapping[str, str] | None = None
    ) -> "Style":
        stream = stream or sys.stdout
        env = os.environ if env is None else env
        capable = (
            stream.isatty() and "NO_COLOR" not in env and env.get("TERM", "") != "dumb"
        )
        encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        return cls(
            color=capable,
            unicode=capable and encoding == "utf8",
            depth=color_depth(env),
            animated=capable and not env.get("CI") and env.get("HINOKI_ANIMATE") != "0",
            light=light_background(env),
            width=shutil.get_terminal_size().columns,
        )

    @property
    def fancy(self) -> bool:
        """Colour and Unicode: the guide rail and the key-driven prompts."""
        return self.color and self.unicode

    def sgr_rgb(self, red: int, green: int, blue: int, *, background: bool) -> str:
        layer = 48 if background else 38
        if self.depth == "truecolor":
            return f"{layer};2;{red};{green};{blue}"
        return f"{layer};5;{xterm256(red, green, blue)}"

    def sgr(self, name: str, *, background: bool = False) -> str:
        dark, light, basic = PALETTE[name]
        hex_color = light if self.light else dark
        if self.depth == "16":
            return str(basic + 10 if background else basic)
        return self.sgr_rgb(*rgb(hex_color), background=background)

    def paint(self, text: str, name: str, *, bold: bool = False) -> str:
        if not self.color:
            return text
        weight = "1;" if bold else ""
        return f"\x1b[{weight}{self.sgr(name)}m{text}{RESET}"

    def bold(self, text: str) -> str:
        """Primary text, emphasised."""
        return self.paint(text, "text", bold=True)

    def text(self, text: str) -> str:
        """Primary text."""
        return self.paint(text, "text")

    def dim(self, text: str) -> str:
        return self.paint(text, "dim")

    def symbol(self, fancy: str, plain: str) -> str:
        return fancy if self.unicode else plain

    def glyph(self, kind: str) -> str:
        fancy, plain, name = GLYPHS[kind]
        bold = kind in {"active", "ok", "bad", "warn"}
        return self.paint(self.symbol(fancy, plain), name, bold=bold)

    def line(self, kind: str, text: str = "") -> str:
        """One line on the guide rail: a glyph in the margin, then ``text``."""
        return f"  {self.glyph(kind)}  {text}".rstrip()

    def wordmark(self, name: str = "Hinoki") -> str:
        """The name in bold primary text."""
        return self.bold(name)

    def prefix(self) -> str:
        """``hinoki`` and an arrow: the brand name that leads the guide rail."""
        arrow = self.paint(self.symbol(RIGHT_ANGLE, ">"), "shade")
        return f"{self.paint('hinoki', 'brand', bold=True)} {arrow}"

    def rail_head(self, title: str, hint: str) -> str:
        """The first line of the guide rail: the prefix, ``title`` and a hint."""
        head = f"{self.prefix()} {self.bold(title)}  {self.dim(hint)}"
        return self.line("start", head)

    def tagline(self, detail: str, *, glint: int | None = None) -> str:
        text = self.paint(TAGLINE, "brand") if glint is None else self.glint(glint)
        return f"{text}  {self.dim(detail)}" if detail else text

    def branch(self) -> tuple[str, str, str]:
        """The top, the side and the bottom of the branch that frames the banner."""
        top, side, bottom = (
            self.paint(self.symbol(fancy, plain), "shade")
            for fancy, plain in zip(BRANCH_UNICODE, BRANCH_ASCII, strict=True)
        )
        return top, side, bottom

    def fact_rows(self, facts: Sequence[tuple[str, str]]) -> list[str]:
        """The ``facts`` as aligned key and value rows under the branch's side.
        A value that does not fit wraps at spaces, or loses its start."""
        side = self.branch()[1]
        key = max(len(name) for name, _ in facts) + 2
        room = max(FACTS_MIN, min(FACTS_MAX, self.width - 5 - key) - 1)
        rows = []
        for name, value in facts:
            label = self.dim(f"{name:<{key}}")
            for part in wrap_value(value, room, self.symbol("\u2026", "...")):
                rows.append(f"  {side}  {label}{self.text(part)}")
                label = " " * key
        return rows

    def header(
        self, name: str, tagline: str, version: str, facts: Sequence[tuple[str, str]]
    ) -> tuple[str, Row, str]:
        """The banner as a branch: what is above the tagline row, the row, and
        what is below it. The tagline drops under the title when it would not
        fit beside it."""
        top, side, bottom = self.branch()
        beside = 5 + len(name) + 2 + len(tagline) + 2 + len(version) < self.width
        title = f"  {top} {self.wordmark(name)}"
        above = "\n" if beside else f"\n{title}  {self.dim(version)}\n"
        lead = f"{title}  " if beside else f"  {side}  "
        tail = f"  {self.dim(version)}" if beside else ""

        def row(glint: int | None) -> str:
            text = self.paint(tagline, "brand") if glint is None else self.glint(glint)
            return f"{lead}{text}{tail}"

        rows = self.fact_rows(facts) if facts else []
        body = "".join(f"\n{line}" for line in [f"  {side}", *rows] if rows)
        return above, row, f"{body}\n  {bottom}\n"

    def lockup(
        self, name: str, tagline: str, version: str, facts: Sequence[tuple[str, str]]
    ) -> str:
        """The branch with the tagline, the version and the ``facts``."""
        above, row, below = self.header(name, tagline, version, facts)
        return f"{above}{row(None)}{below}"

    def banner(
        self, title: str, detail: str = "", facts: Sequence[tuple[str, str]] = ()
    ) -> str:
        """The Hinoki branch with the tagline."""
        if not self.color:
            return f"hinoki {detail} - {title}" if detail else f"hinoki - {title}"
        return self.lockup("Hinoki", TAGLINE, self.shown_detail(title, detail), facts)

    def shown_detail(self, title: str, detail: str) -> str:
        """The detail beside the tagline: with Unicode the guide rail names the
        command, so only the version; without it, both."""
        return detail if self.unicode else f"{title} - {detail}"

    def intro(self, title: str) -> str:
        """The top of the guide rail, under the banner (empty without it)."""
        if not self.fancy:
            return ""
        return f"{self.rail_head(title, 'esc to cancel')}\n{self.line('rail')}"

    def glint(self, centre: int) -> str:
        """The tagline with a highlight around position ``centre``."""
        letters = []
        for index, letter in enumerate(TAGLINE):
            near = abs(index - centre)
            name = "text" if near <= 1 else "light" if near <= GLINT_FADE else "brand"
            letters.append(self.paint(letter, name, bold=near <= 1))
        return "".join(letters)

    def shimmer(
        self,
        title: str,
        detail: str,
        facts: Sequence[tuple[str, str]] = (),
        out: TextIO | None = None,
    ) -> None:
        """Draw the banner, sweeping a highlight across the tagline."""
        out = out or sys.stdout
        above, row, below = self.header(
            "Hinoki", TAGLINE, self.shown_detail(title, detail), facts
        )
        out.write(above)
        for step in range(len(TAGLINE) + GLINT_FADE + 1):
            out.write(f"\r{row(step - 2)}")
            out.flush()
            time.sleep(0.02)
        out.write(f"\r{row(None)}{below}\n")
        out.flush()

    def spinner_frame(self, frame: int, label: str, seconds: float) -> str:
        glyph = SPINNER[frame % len(SPINNER)]
        name = SPINNER_COLORS[frame // 2 % len(SPINNER_COLORS)]
        spin = self.paint(glyph, name, bold=True)
        return f"\r{CLEAR_LINE}  {spin}  {self.timed(label, seconds)}"

    def timed(self, label: str, seconds: float) -> str:
        """``label`` and how long it took, aligned with the other timed steps."""
        return f"{label:<{LABEL_WIDTH}}{self.dim(elapsed(seconds))}"

    @contextmanager
    def spinner(self, label: str, out: TextIO | None = None) -> Iterator[None]:
        """Show a spinner and a timer next to ``label`` while the block runs."""
        out = out or sys.stdout
        if not (self.animated and self.unicode):
            yield
            return
        done = threading.Event()
        started = time.monotonic()

        frames = [0]

        def spin() -> None:
            while not done.wait(0.08):
                seconds = time.monotonic() - started
                hide = HIDE_CURSOR if frames[0] == 0 else ""
                out.write(hide + self.spinner_frame(frames[0], label, seconds))
                out.flush()
                frames[0] += 1

        worker = threading.Thread(target=spin, daemon=True)
        worker.start()
        try:
            yield
        finally:
            done.set()
            worker.join()
            if frames[0]:
                out.write(f"\r{CLEAR_LINE}{SHOW_CURSOR}")
                out.flush()

    def question(self, text: str, default: str) -> str:
        if not self.color:
            return f"{text} [{default}]: " if default else f"{text}: "
        mark = self.paint("?", "brand", bold=True)
        hint = f" {self.paint(f'[{default}]', 'dim')}" if default else ""
        arrow = self.paint(self.symbol(RIGHT_ANGLE, ":"), "brand")
        return f"  {mark} {text}{hint} {arrow} "

    def heading(self, text: str) -> str:
        if not self.color:
            return f"\n{text}"
        return f"\n  {self.paint('?', 'brand', bold=True)} {text}"

    def option(self, index: int, name: str, hint: str, *, default: bool) -> str:
        pointer = self.paint(self.symbol(POINTER, ">"), "brand") if default else " "
        padded = f"{name:<12}"
        label = self.paint(padded, "brand", bold=True) if default else padded
        return f"    {pointer} {index}) {label} {self.paint(hint, 'dim')}".rstrip()

    def status(self, kind: str, plain: str, text: str) -> str:
        """A result line: a coloured glyph on a terminal, a plain prefix otherwise."""
        return self.line(kind, text) if self.color else f"{plain}{text}"

    def ok(self, text: str) -> str:
        return self.status("ok", "", text)

    def skipped(self, text: str) -> str:
        return self.status("skip", "skipped: ", text)

    def warning(self, text: str) -> str:
        return self.status("warn", "warning: ", text)

    def error(self, text: str) -> str:
        return self.status("bad", "error: ", text)
