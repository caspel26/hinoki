"""Key-driven prompts on a guide rail, for ``hinoki new`` on a capable terminal.

Each question is drawn as a small block that is redrawn in place on every key and
settles into a one-line summary once answered. Only used when standard input is a
terminal that can be put in cbreak mode; otherwise ``hinoki new`` asks line by line.
"""

import codecs
import os
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Protocol, TextIO

from hinoki.terminal import HIDE_CURSOR, SHOW_CURSOR, Style, fit

if sys.platform != "win32":
    import termios
    import tty

CURSOR = "\x1b[7m \x1b[27m"
DOT = "\u25cf"
CIRCLE = "\u25cb"
SUMMARY_WIDTH = 12
KEYS = {
    "\r": "enter",
    "\n": "enter",
    "\x7f": "backspace",
    "\x08": "backspace",
    "\x15": "clear",
    "\t": "down",
    "\x1b[A": "up",
    "\x1b[B": "down",
    "\x1b[C": "right",
    "\x1b[D": "left",
    "\x1bOA": "up",
    "\x1bOB": "down",
    "\x1bOC": "right",
    "\x1bOD": "left",
    "\x1b": "escape",
}
CANCEL = {"\x03", "escape"}

Check = Callable[[str], str | None]


def tokens(chunk: str) -> Iterator[str]:
    """Split typed text into keys: one character or one escape sequence each."""
    index = 0
    while index < len(chunk):
        size = 1
        if chunk.startswith(("\x1b[", "\x1bO"), index) and index + 2 < len(chunk):
            size = 3
        key = chunk[index : index + size]
        index += size
        yield KEYS.get(key, key)


def read_keys(fd: int) -> Iterator[str]:
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    while chunk := os.read(fd, 64):
        yield from tokens(decoder.decode(chunk))


def keys_available() -> bool:
    return sys.platform != "win32" and sys.stdin.isatty()


@contextmanager
def raw_keys(out: TextIO) -> Iterator[Iterator[str]]:
    """Read single keys from the terminal, restoring its mode afterwards."""
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    out.write(HIDE_CURSOR)
    out.flush()
    try:
        yield read_keys(fd)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        out.write(SHOW_CURSOR)
        out.flush()


class Widget(Protocol):
    summary: str

    def render(self, style: Style) -> list[str]: ...

    def handle(self, key: str) -> bool: ...

    def value(self) -> str: ...


def head(style: Style, label: str, error: str | None) -> str:
    if error:
        return style.line("warn", style.paint(label, "warn", bold=True))
    return style.line("active", style.bold(label))


def foot(style: Style, hint: str, error: str | None) -> str:
    return style.line("end", style.paint(error, "warn") if error else style.dim(hint))


class Text:
    def __init__(self, label: str, summary: str, default: str, check: Check) -> None:
        self.label, self.summary, self.default, self.check = (
            label,
            summary,
            default,
            check,
        )
        self.hint = "enter to confirm"
        self.typed = ""
        self.error: str | None = None

    def field(self, style: Style) -> str:
        if self.typed:
            return f"{self.typed[-60:]}{CURSOR}"
        if self.default:
            return f"\x1b[7m{self.default[0]}\x1b[27m{style.dim(self.default[1:])}"
        return CURSOR

    def render(self, style: Style) -> list[str]:
        return [
            head(style, self.label, self.error),
            style.line("rail", self.field(style)),
            foot(style, self.hint, self.error),
        ]

    def handle(self, key: str) -> bool:
        if key == "enter":
            self.error = self.check(self.value())
            return self.error is None
        self.error = None
        if key == "backspace":
            self.typed = self.typed[:-1]
        elif key == "clear":
            self.typed = ""
        elif len(key) == 1 and key.isprintable():
            self.typed += key
        return False

    def value(self) -> str:
        return self.typed.strip() or self.default


class Choice:
    def __init__(
        self, label: str, summary: str, options: Sequence[tuple[str, str]], default: str
    ) -> None:
        self.label, self.summary, self.options = label, summary, options
        self.index = [name for name, _ in options].index(default)

    def render(self, style: Style) -> list[str]:
        width = max(len(name) for name, _ in self.options) + 2
        lines = [head(style, self.label, None)]
        for index, (name, hint) in enumerate(self.options):
            if index == self.index:
                mark = style.paint(DOT, "brand", bold=True)
                text = style.bold(f"{name:<{width}}")
            else:
                mark, text = style.dim(CIRCLE), style.dim(f"{name:<{width}}")
            lines.append(style.line("rail", f"{mark} {text}{style.dim(hint)}"))
        hint = "\u2191\u2193 to move \u00b7 1-9 to pick \u00b7 enter to confirm"
        return [*lines, foot(style, hint, None)]

    def handle(self, key: str) -> bool:
        count = len(self.options)
        if key in {"up", "k"}:
            self.index = (self.index - 1) % count
        elif key in {"down", "j"}:
            self.index = (self.index + 1) % count
        elif key.isdigit() and 1 <= int(key) <= count:
            self.index = int(key) - 1
        return key == "enter"

    def value(self) -> str:
        return self.options[self.index][0]


class Confirm:
    def __init__(self, label: str, summary: str, default: bool) -> None:
        self.label, self.summary, self.answer = label, summary, default

    def render(self, style: Style) -> list[str]:
        def option(text: str, chosen: bool) -> str:
            if chosen:
                return f"{style.paint(DOT, 'brand', bold=True)} {style.bold(text)}"
            return style.dim(f"{CIRCLE} {text}")

        choices = f"{option('Yes', self.answer)}  {style.dim('/')}  "
        choices += option("No", not self.answer)
        hint = "\u2190\u2192 or y/n \u00b7 enter to confirm"
        return [
            head(style, self.label, None),
            style.line("rail", choices),
            foot(style, hint, None),
        ]

    def handle(self, key: str) -> bool:
        if key in {"left", "right", "up", "down", "h", "l"}:
            self.answer = not self.answer
        elif key.lower() in {"y", "n"}:
            self.answer = key.lower() == "y"
        return key == "enter"

    def value(self) -> str:
        return "yes" if self.answer else "no"


class Prompter:
    """Asks questions with key-driven widgets, redrawing each one in place."""

    def __init__(
        self, style: Style, keys: Iterator[str], out: TextIO, width: int = 80
    ) -> None:
        self.style, self.keys, self.out, self.width = style, keys, out, width
        self.height = 0

    def draw(self, lines: list[str]) -> None:
        if self.height > 1:
            self.out.write(f"\x1b[{self.height - 1}F")
        elif self.height == 1:
            self.out.write("\r")
        fitted = [fit(line, self.width - 1) for line in lines]
        self.out.write("\x1b[J" + "\n".join(fitted))
        self.out.flush()
        self.height = len(lines)

    def settle(self, mark: str, summary: str, value: str) -> None:
        label = self.style.dim(f"{summary:<{SUMMARY_WIDTH}}")
        shown = self.style.text(value) if mark == "done" else value
        self.draw([self.style.line(mark, f"{label} {shown}")])
        self.out.write("\n")
        self.height = 0

    def run(self, widget: Widget) -> str:
        self.draw(widget.render(self.style))
        for key in self.keys:
            if key in CANCEL:
                raise KeyboardInterrupt
            if key == "\x04":
                break
            if widget.handle(key):
                return widget.value()
            self.draw(widget.render(self.style))
        raise EOFError

    def ask(self, widget: Widget) -> str:
        try:
            answer = self.run(widget)
        except (KeyboardInterrupt, EOFError):
            self.settle("cancel", widget.summary, self.style.dim("cancelled"))
            raise
        self.settle("done", widget.summary, answer)
        return answer

    def text(
        self, label: str, summary: str, default: str, check: Check, hint: str = ""
    ) -> str:
        widget = Text(label, summary, default, check)
        widget.hint = f"{hint} \u00b7 enter to confirm" if hint else widget.hint
        return self.ask(widget)

    def choice(
        self, label: str, summary: str, options: Sequence[tuple[str, str]], default: str
    ) -> str:
        return self.ask(Choice(label, summary, options, default))

    def confirm(self, label: str, summary: str, default: bool) -> bool:
        return self.ask(Confirm(label, summary, default)) == "yes"
