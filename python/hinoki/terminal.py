"""Small terminal styling for the interactive commands (ANSI, standard library only).

Colour and Unicode symbols are used only on a capable terminal. Piped output,
``NO_COLOR`` and ``TERM=dumb`` get plain ASCII text with no escape sequences.
"""

import os
import sys
from collections.abc import Mapping
from typing import TextIO

RESET = "\x1b[0m"
BRAND = "1;32"
DIM = "2"
ACCENT = "36"
GOOD = "32"
WARN = "33"
BAD = "31"


class Style:
    """Paints text and chooses symbols for one output stream."""

    def __init__(self, *, color: bool = False, unicode: bool = False) -> None:
        self.color = color
        self.unicode = unicode

    @classmethod
    def detect(
        cls, stream: TextIO | None = None, env: Mapping[str, str] | None = None
    ) -> "Style":
        stream = stream or sys.stdout
        env = os.environ if env is None else env
        capable = (
            stream.isatty() and "NO_COLOR" not in env and env.get("TERM", "") != "dumb"
        )
        utf8 = (getattr(stream, "encoding", "") or "").lower().replace(
            "-", ""
        ) == "utf8"
        return cls(color=capable, unicode=capable and utf8)

    def paint(self, text: str, code: str) -> str:
        return f"\x1b[{code}m{text}{RESET}" if self.color else text

    def symbol(self, fancy: str, plain: str) -> str:
        return fancy if self.unicode else plain

    def banner(self, title: str) -> str:
        if not self.color:
            return f"hinoki - {title}"
        rule = self.symbol("─", "-") * 40
        head = f"{self.paint('hinoki', BRAND)} {self.paint('·', DIM)} {title}"
        return f"\n  {head}\n  {self.paint(rule, DIM)}\n"

    def question(self, text: str, default: str) -> str:
        if not self.color:
            return f"{text} [{default}]: " if default else f"{text}: "
        mark = self.paint("?", ACCENT)
        hint = f" {self.paint(f'[{default}]', DIM)}" if default else ""
        return f"  {mark} {text}{hint} {self.symbol('\u203a', ':')} "

    def heading(self, text: str) -> str:
        return f"\n  {self.paint('?', ACCENT)} {text}" if self.color else f"\n{text}"

    def option(self, index: int, name: str, hint: str, *, default: bool) -> str:
        pointer = self.paint(self.symbol("\u276f", ">"), ACCENT) if default else " "
        label = self.paint(f"{name:<12}", ACCENT) if default else f"{name:<12}"
        return f"    {pointer} {index}) {label} {self.paint(hint, DIM)}".rstrip()

    def status(self, symbols: tuple[str, str], code: str, plain: str, text: str) -> str:
        """A result line: a coloured symbol on a terminal, a plain prefix otherwise."""
        if not self.color:
            return f"{plain}{text}"
        return f"  {self.paint(self.symbol(*symbols), code)} {text}"

    def ok(self, text: str) -> str:
        return self.status(("✔", "ok"), GOOD, "", text)

    def skipped(self, text: str) -> str:
        return self.status(("\u2013", "-"), WARN, "skipped: ", text)

    def warning(self, text: str) -> str:
        return self.status(("!", "!"), WARN, "warning: ", text)

    def error(self, text: str) -> str:
        return self.status(("✘", "x"), BAD, "error: ", text)

    def dim(self, text: str) -> str:
        return self.paint(text, DIM)
