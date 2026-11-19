"""Small terminal styling for the interactive commands (ANSI, standard library only).

Colours are the site palette (``theme/static/css/tokens.css``, dark theme). Colour
and Unicode symbols are used only on a capable terminal; piped output,
``NO_COLOR`` and ``TERM=dumb`` get plain ASCII text with no escape sequences.
"""

import os
import sys
from collections.abc import Mapping
from typing import TextIO

RESET = "\x1b[0m"
RIGHT_ANGLE = "\u203a"
POINTER = "\u276f"

# name: (hex, nearest basic ANSI foreground code)
PALETTE: dict[str, tuple[str, int]] = {
    "brand": ("#b98cff", 35),
    "light": ("#cdaefc", 95),
    "shade": ("#7a55c4", 35),
    "text": ("#f4f1ec", 97),
    "dim": ("#8a8a98", 90),
    "good": ("#8fdc98", 32),
    "warn": ("#ffcc80", 33),
    "bad": ("#ff7a8a", 31),
}


def rgb(hex_color: str) -> tuple[int, int, int]:
    return (
        int(hex_color[1:3], 16),
        int(hex_color[3:5], 16),
        int(hex_color[5:7], 16),
    )


def xterm256(red: int, green: int, blue: int) -> int:
    """The nearest colour in the 6x6x6 cube of the 256-colour palette."""
    return 16 + 36 * round(red / 51) + 6 * round(green / 51) + round(blue / 51)


class Style:
    """Paints text and chooses symbols for one output stream.

    ``depth`` is ``"truecolor"``, ``"256"`` or ``"16"``.
    """

    def __init__(
        self, *, color: bool = False, unicode: bool = False, depth: str = "16"
    ) -> None:
        self.color = color
        self.unicode = unicode
        self.depth = depth

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
        if env.get("COLORTERM", "").lower() in {"truecolor", "24bit"}:
            depth = "truecolor"
        elif "256" in env.get("TERM", ""):
            depth = "256"
        else:
            depth = "16"
        return cls(color=capable, unicode=capable and encoding == "utf8", depth=depth)

    def sgr(self, name: str, *, background: bool = False) -> str:
        hex_color, basic = PALETTE[name]
        if self.depth == "truecolor":
            red, green, blue = rgb(hex_color)
            return f"{48 if background else 38};2;{red};{green};{blue}"
        if self.depth == "256":
            return f"{48 if background else 38};5;{xterm256(*rgb(hex_color))}"
        return str(basic + 10 if background else basic)

    def paint(self, text: str, name: str, *, bold: bool = False) -> str:
        if not self.color:
            return text
        weight = "1;" if bold else ""
        return f"\x1b[{weight}{self.sgr(name)}m{text}{RESET}"

    def symbol(self, fancy: str, plain: str) -> str:
        return fancy if self.unicode else plain

    def banner(self, title: str) -> str:
        """The wordmark of the site banner: white lower-case letters, violet i's."""
        if not self.color:
            return f"hinoki - {title}"
        brand = self.paint("hinoki", "text", bold=True)
        if not self.unicode:
            return f"\n  {brand}  {self.paint(title, 'dim')}\n"
        letters = " ".join(
            self.paint(letter, "brand" if letter == "i" else "text", bold=True)
            for letter in "hinoki"
        )
        rule = self.paint("\u2500" * 21, "shade")
        return "\n".join(
            [
                "",
                f"    {letters}",
                f"    {rule}",
                f"    {self.paint('Python API framework', 'brand')}",
                f"    {self.paint(title, 'dim')}",
                "",
            ]
        )

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

    def status(self, symbols: tuple[str, str], name: str, plain: str, text: str) -> str:
        """A result line: a coloured symbol on a terminal, a plain prefix otherwise."""
        if not self.color:
            return f"{plain}{text}"
        return f"  {self.paint(self.symbol(*symbols), name)} {text}"

    def ok(self, text: str) -> str:
        return self.status(("\u2714", "ok"), "good", "", text)

    def skipped(self, text: str) -> str:
        return self.status(("\u2013", "-"), "warn", "skipped: ", text)

    def warning(self, text: str) -> str:
        return self.status(("!", "!"), "warn", "warning: ", text)

    def error(self, text: str) -> str:
        return self.status(("\u2718", "x"), "bad", "error: ", text)

    def dim(self, text: str) -> str:
        return self.paint(text, "dim")
