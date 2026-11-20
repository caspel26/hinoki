"""Record ``assets/readme/demo.gif`` and ``demo.png`` from a real terminal session.

Run from the repository root after ``uv sync --locked``, with port 8000 free::

    uv run --with pyte==0.8.2 --with pillow==12.0.0 --with fonttools==4.60.1 \\
        --with brotli==1.1.0 python scripts/readme_demo.py

The script runs the real ``hinoki`` CLI in a pseudo-terminal inside a throw-away
directory: the interactive ``hinoki new`` (answered with key presses), ``hinoki
dev`` (stopped with Ctrl-C), then ``hinoki check`` on a route group without
``atomic=True`` and again once the script has added it. A terminal emulator (pyte)
keeps the screen; every frame of the GIF is that screen drawn with IBM Plex Mono
from ``theme/static/fonts`` inside a drawn window, between a title card made from
``lockup-dark.svg``. Glyphs Plex Mono lacks (the ``◢◤`` mark, box lines, spinners)
are drawn as shapes. Only the timing is edited: waits are shortened and repeated
spinner frames are dropped. The output is never rewritten, so re-run the script
after a prompt or a message changes.

The children get ``NOTES_ALLOWED_HOSTS`` (a documented setting) so ``hinoki dev``
does not log the ``allowed_hosts is '*'`` warning meant for deployments, and
``hinoki new`` skips ``uv sync``: the commands run with this checkout's ``hinoki``.

It needs ``ffmpeg``, ``fc-match``, ``rsvg-convert``, ``git`` and ``uv`` on the PATH.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import os
import select
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import termios
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyte
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets" / "readme"
FONTS = ROOT / "theme" / "static" / "fonts"
MAX_BYTES = 600_000
PORT = 8000

COLS, ROWS = 100, 24
CELL_W, CELL_H, FONT_SIZE = 9, 22, 15
BAR, INSET_X, INSET_Y = 36, 18, 14  # title bar; terminal padding inside the window
WIN_W = COLS * CELL_W + 2 * INSET_X
WIN_H = BAR + ROWS * CELL_H + 2 * INSET_Y
MARGIN_X, MARGIN_Y, RADIUS = 40, 34, 12  # backdrop around the rounded window
WIDTH, HEIGHT = WIN_W + 2 * MARGIN_X, WIN_H + 2 * MARGIN_Y
PAD_X = MARGIN_X + INSET_X
PAD_Y = MARGIN_Y + BAR + INSET_Y
TITLE = "hinoki \u2014 notes"
TAGLINE = "Typed Python APIs. Architecture rules you can run."
SITE = "caspel26.github.io/hinoki"
CARD_MS = 1600  # the title card before and after the session

ENTER, DOWN, CTRL_C = "\r", "\x1b[B", "\x03"
SGR_256 = 5  # 38;5;n selects an indexed colour, 38;2;r;g;b a true colour
BRAILLE = range(0x2800, 0x2900)
TYPE_MS = 45  # per typed character of a command line
OUTPUT_MS = 60  # per new screen while a program prints
SPIN_MS = 80  # per kept frame of a spinner or the banner shimmer
SPIN_FRAMES = 12  # frames kept of any run where one line animates
SETTLE_S = 0.35  # quiet time before a key is pressed
QUIET_S, BURST_S = 0.04, 0.08  # snapshot after this quiet, or this often

COLORS = {
    "fg": "#e6e0f5",
    "bg": "#1c1726",
    "bar": "#26212f",
    "border": "#3a3448",
    "title": "#9d9ca9",
    "cursor": "#b98cff",
    "prompt": "#b9a0e7",
    "comment": "#9d9ca9",
    "backdrop": "#17171c",
    "backdrop_glow": "#2a2734",
}
LIGHTS = ("#ff5f57", "#febc2e", "#28c840")  # close, minimise, zoom
# Named ANSI colours mapped to the dark values of ``hinoki.terminal.PALETTE``.
ANSI = {
    "black": "#1d1a24",
    "red": "#ff7a8a",
    "green": "#86cfb3",
    "brown": "#e0bc7d",
    "yellow": "#e0bc7d",
    "blue": "#8fb8e0",
    "magenta": "#b9a0e7",
    "cyan": "#8ec5ff",
    "white": "#eeedf2",
}

SECURITY = '''"""Who may change notes."""

from hinoki import Access, BearerAuth, Caller, Permission


class NotePermission(Permission):
    WRITE = "notes:write"


class Tokens(BearerAuth[str]):
    async def resolve_token(self, token: str) -> Caller[str] | None:
        if token != "demo-token":
            return None
        return Caller(id="ann", user="ann", permissions={NotePermission.WRITE})


editors = Access(auth=Tokens(), permissions=[NotePermission.WRITE])
'''

MODEL = '''"""Models owned by the project app."""

from uuid import UUID, uuid4

from hinoki.orm import Model, field, pk


class Note(Model):
    id: UUID = pk(default_factory=uuid4)
    title: str = field(max_length=80)
    body: str = ""
'''

GROUP = 'notes = RouteGroup("/notes", access=editors)'
API = f'''"""Endpoints."""

from typing import Annotated

from hinoki import Field, RouteGroup, Schema, derive, http

from notes.models import Note
from notes.security import editors


class Health(Schema):
    status: str


@http.get("/health")
def health() -> Health:
    return Health(status="ok")


class NoteIn(Schema):
    title: Annotated[str, Field(min_length=1, max_length=80)]
    body: Annotated[str, Field(max_length=2000)] = ""


@derive(Note)
class NoteOut(Schema): ...


{GROUP}


@notes.post("/", status=201)
async def create_note(data: NoteIn) -> NoteOut:
    return NoteOut.from_model(await Note.acreate(data))
'''

ROUTES = '''"""The project's own routes."""

from notes.api import health, notes

routes = [health, notes]
'''

Cell = tuple[str, str, str, bool, bool, bool]  # text, fg, bg, bold, reverse, faint
Row = tuple[Cell, ...]


class DemoError(RuntimeError):
    """The session did not go as recorded; the GIF would be misleading."""


@dataclass(frozen=True)
class Key:
    """A key press, sent once the screen is quiet (and shows ``wait_for``)."""

    data: str
    pause_ms: int = 600  # how long the GIF shows the screen before the key
    wait_for: str = ""
    settle_s: float = SETTLE_S  # real quiet time before the key is sent


@dataclass
class Scene:
    """One command run in the terminal."""

    argv: list[str]
    keys: list[Key] = field(default_factory=list)
    hold_ms: int = 900
    status: int | None = 0  # expected exit status; ``None`` when interrupted
    timeout_s: float = 120.0

    @property
    def command(self) -> str:
        return shlex.join(self.argv)


@dataclass
class Frame:
    rows: tuple[Row, ...]
    cursor: tuple[int, int] | None
    duration: int
    pinned: bool = False  # a key press or the end of a scene: never dropped
    card: str = ""  # "intro" or "outro": the title card instead of the screen


class DemoScreen(pyte.Screen):  # type: ignore[misc]
    """A pyte screen that keeps SGR 2 (faint) in the otherwise unused blink flag."""

    def select_graphic_rendition(self, *attrs: int, **kwargs: Any) -> None:
        if kwargs.get("private"):
            return
        super().select_graphic_rendition(*faint_as_blink(attrs))


def faint_as_blink(attrs: Sequence[int]) -> list[int]:
    """Rewrite SGR 2/22 to 5/25, leaving colour arguments of 38/48 untouched."""
    out: list[int] = []
    index = 0
    while index < len(attrs):
        attr = attrs[index]
        if attr in (38, 48) and index + 1 < len(attrs):
            width = 3 if attrs[index + 1] == SGR_256 else 5
            out.extend(attrs[index : index + width])
            index += width
            continue
        out.extend({2: [5], 22: [22, 25]}.get(attr, [attr]))
        index += 1
    return out


def take_terminal() -> None:
    """In the child: start a session with the pty as its controlling terminal."""
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def read_chunk(master: int, timeout: float) -> bytes | None:
    """Bytes the program wrote, ``b""`` when it wrote nothing, ``None`` at EOF."""
    ready, _, _ = select.select([master], [], [], timeout)
    if not ready:
        return b""
    try:
        return os.read(master, 65536) or None
    except OSError:
        return None


class Session:
    """A terminal screen shared by every scene, recorded as frames."""

    def __init__(self, env: dict[str, str]) -> None:
        self.env = env
        self.screen = DemoScreen(COLS, ROWS)
        self.stream = pyte.ByteStream(self.screen)
        self.frames: list[Frame] = []
        self.rows: dict[Row, Row] = {}
        self.prompted = False

    def text(self) -> str:
        return "\n".join(self.screen.display)

    def feed(self, data: str | bytes) -> None:
        self.stream.feed(data.encode() if isinstance(data, str) else data)

    def snapshot(self) -> tuple[tuple[Row, ...], tuple[int, int] | None]:
        rows = []
        for y in range(ROWS):
            line = self.screen.buffer[y]
            row = tuple(cell(line[x]) for x in range(COLS))
            rows.append(self.rows.setdefault(row, row))
        cursor = self.screen.cursor
        return tuple(rows), None if cursor.hidden else (cursor.x, cursor.y)

    def snap(self, duration: int, *, pinned: bool = False) -> None:
        rows, cursor = self.snapshot()
        last = self.frames[-1] if self.frames else None
        if last and last.rows == rows and last.cursor == cursor:
            last.duration += duration
            last.pinned = last.pinned or pinned
            return
        self.frames.append(Frame(rows, cursor, duration, pinned))

    def card(self, kind: str, duration: int) -> None:
        """Show the title card (drawn by the painter, not terminal output)."""
        rows, _ = self.snapshot()
        self.frames.append(Frame(rows, None, duration, pinned=True, card=kind))

    def hold(self, duration: int) -> None:
        """Show the current screen for at least ``duration`` and keep the frame."""
        self.snap(0)
        self.frames[-1].duration = max(self.frames[-1].duration, duration)
        self.frames[-1].pinned = True

    def prompt(self) -> None:
        """Draw the shell prompt on a fresh line, as a shell does after a command."""
        if self.prompted:
            return
        if self.screen.cursor.x:
            self.feed("\r\n")
        self.feed(f"{sgr(COLORS['prompt'])}${RESET} ")
        self.prompted = True

    def type_line(self, text: str, *, comment: bool = False) -> None:
        """Type ``text`` at the prompt (there is no shell: the line is drawn)."""
        self.prompt()
        if comment:
            self.feed(sgr(COLORS["comment"]))
        self.hold(400)
        for end in range(2, len(text) + 2, 2):
            self.feed(text[end - 2 : end])
            self.snap(TYPE_MS * 2, pinned=True)
        self.feed(RESET)
        self.hold(350)
        self.feed("\r\n")
        self.prompted = False

    def run(self, scene: Scene, cwd: Path) -> int:
        """Type and run ``scene`` in a fresh pty sized like the screen."""
        self.type_line(scene.command)
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
        process = subprocess.Popen(
            scene.argv,
            cwd=cwd,
            env=self.env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            preexec_fn=take_terminal,  # noqa: PLW1509 - single-threaded; the child needs a controlling tty
        )
        os.close(slave)
        try:
            Pump(self, master, process, scene).run()
        finally:
            os.close(master)
            stop_session(process)
        self.prompt()
        self.hold(scene.hold_ms)
        check_status(scene, process.returncode, self.text())
        return process.returncode


class Pump:
    """Copy a program's output to the screen, take frames and press keys."""

    def __init__(
        self,
        session: Session,
        master: int,
        process: subprocess.Popen[bytes],
        scene: Scene,
    ) -> None:
        self.session = session
        self.master = master
        self.process = process
        self.scene = scene
        self.keys = list(scene.keys)
        now = time.monotonic()
        self.deadline = now + scene.timeout_s
        self.last_data = self.last_snap = now
        self.dirty = False

    def run(self) -> None:
        while self.step():
            if time.monotonic() > self.deadline:
                raise DemoError(f"{self.scene.command} timed out")
        if self.dirty:
            self.session.snap(OUTPUT_MS)

    def step(self) -> bool:
        """Handle one read; ``False`` once the program has finished."""
        data = read_chunk(self.master, 0.02)
        now = time.monotonic()
        if data is None:
            return False
        if data:
            self.session.feed(data)
            self.dirty, self.last_data = True, now
        quiet = now - self.last_data
        if self.dirty and (quiet >= QUIET_S or now - self.last_snap >= BURST_S):
            self.session.snap(OUTPUT_MS)
            self.dirty, self.last_snap = False, now
        if self.keys and self.ready(self.keys[0], quiet):
            self.press(self.keys.pop(0))
        return not (self.process.poll() is not None and quiet >= SETTLE_S)

    def ready(self, key: Key, quiet: float) -> bool:
        if quiet < key.settle_s:
            return False
        return not key.wait_for or key.wait_for in self.session.text()

    def press(self, key: Key) -> None:
        self.session.hold(key.pause_ms)
        os.write(self.master, key.data.encode())
        self.last_data = time.monotonic()


def stop_session(process: subprocess.Popen[bytes]) -> None:
    """Stop whatever the command left running in its session (server workers)."""
    signum = signal.SIGKILL if process.poll() is None else signal.SIGTERM
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signum)
    process.wait()


def check_status(scene: Scene, status: int, screen: str) -> None:
    if scene.status is not None and status != scene.status:
        expected = f"{scene.command} exited with {status}, not {scene.status}"
        raise DemoError(f"{expected}; the screen shows:\n{screen.rstrip()}")


def cell(char: Any) -> Cell:
    return (char.data, char.fg, char.bg, char.bold, char.reverse, char.blink)


RESET = "\x1b[0m"


def sgr(hex_color: str) -> str:
    red, green, blue = rgb(hex_color)
    return f"\x1b[38;2;{red};{green};{blue}m"


def rgb(hex_color: str) -> tuple[int, int, int]:
    value = hex_color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def child_env(hinoki: Path) -> dict[str, str]:
    """The user's environment as a fresh colour terminal would give it."""
    drop = {
        "NO_COLOR",
        "CI",
        "TERM_PROGRAM",
        "KITTY_WINDOW_ID",
        "TMUX",
        "STY",
        "VIRTUAL_ENV",
        "COLORFGBG",
        "UV_PROJECT_ENVIRONMENT",
        "HINOKI_ANIMATE",
    }
    env = {key: value for key, value in os.environ.items() if key not in drop}
    path = f"{hinoki.parent}{os.pathsep}{env.get('PATH', '')}"
    return env | {
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
        "LANG": "C.UTF-8",
        "HINOKI_IMAGES": "0",
        "NOTES_ALLOWED_HOSTS": "127.0.0.1,localhost",
        "PATH": path,
    }


def new_keys() -> list[Key]:
    """Answers to ``hinoki new``: name ``notes``, SQLite, no ``uv sync``."""
    name = [Key("n", 900), *(Key(char, 130) for char in "otes")]
    return [
        *name,
        Key(ENTER, 500),  # name
        Key(ENTER, 700),  # parent directory: .
        Key(DOWN, 1100),  # database: none -> sqlite
        Key(ENTER, 600),
        Key(ENTER, 1100),  # server: kiso
        *(Key(ENTER, 450) for _ in range(3)),  # agent files, skills, git
        Key("n", 700),  # install: this checkout's hinoki is already on the PATH
        Key(ENTER, 450),
    ]


def write_api(project: Path) -> None:
    package = project / "notes"
    (package / "security.py").write_text(SECURITY)
    (package / "models.py").write_text(MODEL)
    (package / "api.py").write_text(API)
    (package / "routes.py").write_text(ROUTES)


def add_atomic(project: Path) -> None:
    api = project / "notes" / "api.py"
    api.write_text(API.replace(GROUP, GROUP.replace(")", ", atomic=True)")))


def record(workdir: Path, env: dict[str, str]) -> list[Frame]:
    """Run every scene and return the frames, failing on unexpected output."""
    session = Session(env)
    session.card("intro", CARD_MS)
    new = Scene(["hinoki", "new"], keys=new_keys(), hold_ms=2600, timeout_s=600)
    session.run(new, workdir)
    project = workdir / "notes"
    if not (project / "hinoki.toml").is_file():
        raise DemoError("hinoki new did not create notes/hinoki.toml")
    session.type_line("cd notes")
    session.run(Scene(["clear"], hold_ms=100), project)
    # The worker imports the app after the banner; stop it once it is serving.
    stop = Key(CTRL_C, 2400, wait_for="listening on", settle_s=4.0)
    session.run(
        Scene(["hinoki", "dev"], keys=[stop], hold_ms=500, status=None), project
    )
    if "Traceback" in session.text() or "WARNING" in session.text():
        raise DemoError("hinoki dev printed a warning or a traceback")
    session.type_line("# notes/api.py: a RouteGroup with POST /notes", comment=True)
    write_api(project)
    session.run(Scene(["hinoki", "check"], hold_ms=3200, status=1), project)
    if "atomic=True" not in session.text():
        raise DemoError("hinoki check did not ask for atomic=True")
    session.type_line("# add atomic=True to the RouteGroup", comment=True)
    add_atomic(project)
    session.run(Scene(["hinoki", "check"], hold_ms=3000), project)
    if "0 violated" not in session.text():
        raise DemoError("hinoki check did not report 0 violated")
    session.card("outro", CARD_MS + 600)
    return session.frames


def changed_rows(before: Frame, after: Frame) -> set[int]:
    return {y for y in range(ROWS) if before.rows[y] is not after.rows[y]}


def single_row(frames: list[Frame], index: int) -> int | None:
    """The only line frame ``index`` changes, if it changes one and is not pinned."""
    rows = changed_rows(frames[index - 1], frames[index])
    if len(rows) != 1 or frames[index].pinned:
        return None
    return next(iter(rows))


def spinner_runs(frames: list[Frame]) -> Iterator[range]:
    """Index ranges of frames in which only one line changes, frame after frame."""
    start, line = 0, None
    for index in range(1, len(frames)):
        row = single_row(frames, index)
        if row is not None and row == line:
            continue
        if index - start > SPIN_FRAMES:
            yield range(start, index)
        start, line = (index - 1, row) if row is not None else (index, None)
    if len(frames) - start > SPIN_FRAMES:
        yield range(start, len(frames))


def compress(frames: list[Frame]) -> list[Frame]:
    """Keep at most ``SPIN_FRAMES`` evenly spaced frames of each spinner run."""
    dropped: set[int] = set()
    for run in spinner_runs(frames):
        step = len(run) / SPIN_FRAMES
        kept = {run[int(i * step)] for i in range(SPIN_FRAMES)} | {run[-1]}
        dropped.update(index for index in run if index not in kept)
        for index in kept - {run[-1]}:
            frames[index].duration = SPIN_MS
    return [frame for index, frame in enumerate(frames) if index not in dropped]


def plex(name: str, scratch: Path) -> Path:
    """Convert a WOFF2 font of the docs theme to a TTF Pillow can load."""
    font = TTFont(FONTS / name)
    font.flavor = None
    out = scratch / Path(name).with_suffix(".ttf").name
    font.save(out)
    return out


def fallback_font(char: str) -> Path:
    """A monospace font with ``char``, for code points IBM Plex Mono lacks."""
    pattern = f"monospace:style=Regular:charset={ord(char):x}"
    found = subprocess.run(
        ["fc-match", "-s", "-f", "%{file}\n", pattern],
        capture_output=True,
        text=True,
        check=True,
    )
    for line in found.stdout.splitlines()[:20]:
        if ord(char) in TTFont(line, fontNumber=0).getBestCmap():
            return Path(line)
    raise DemoError(f"no installed font draws U+{ord(char):04X}")


def blend(color: str, toward: str, amount: float) -> str:
    mixed = (
        round(a + (b - a) * amount)
        for a, b in zip(rgb(color), rgb(toward), strict=True)
    )
    return "#{:02x}{:02x}{:02x}".format(*mixed)


def resolve(color: str, *, background: bool) -> str:
    if color == "default":
        return COLORS["bg" if background else "fg"]
    name = color.removeprefix("bright")
    if name in ANSI:
        return ANSI[name]
    return f"#{color}"


def cell_colors(item: Cell) -> tuple[str, str]:
    _, fg, bg, _, reverse, faint = item
    front, back = resolve(fg, background=False), resolve(bg, background=True)
    if reverse:
        front, back = back, front
    if faint:
        front = blend(front, back, 0.45)
    return front, back


def backdrop() -> Image.Image:
    """A dark backdrop, lighter towards the centre, with a faint violet orb."""
    glow = Image.radial_gradient("L").resize((WIDTH, HEIGHT))
    image = Image.composite(
        Image.new("RGBA", (WIDTH, HEIGHT), COLORS["backdrop"]),
        Image.new("RGBA", (WIDTH, HEIGHT), COLORS["backdrop_glow"]),
        glow,
    )
    orb = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(orb).ellipse((-120, -160, 300, 220), fill=(122, 85, 196, 60))
    image.alpha_composite(orb.filter(ImageFilter.GaussianBlur(80)))
    return image


def light(color: str, size: int = 12, scale: int = 8) -> Image.Image:
    """One traffic light: a smooth circle with a slightly darker ring."""
    big = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    ring = blend(color, "#000000", 0.25)
    ImageDraw.Draw(big).ellipse(
        (0, 0, size * scale - 1, size * scale - 1),
        fill=color,
        outline=ring,
        width=scale // 2,
    )
    return big.resize((size, size), Image.Resampling.LANCZOS)


def lockup(scratch: Path, *, width: int = 0, height: int = 0) -> Image.Image:
    """``lockup-dark.svg`` (mark and wordmark) as an RGBA image of the given size."""
    out = scratch / f"lockup-{width}x{height}.png"
    size = ["-w", str(width)] if width else ["-h", str(height)]
    source = ASSETS / "lockup-dark.svg"
    subprocess.run(["rsvg-convert", *size, str(source), "-o", str(out)], check=True)
    return Image.open(out).convert("RGBA")


class Painter:
    """Draws frames: the window on its backdrop, then the cell grid in IBM Plex Mono."""

    def __init__(self, scratch: Path) -> None:
        regular = plex("ibm-plex-mono-latin-400.woff2", scratch)
        bold = plex("ibm-plex-mono-latin-500.woff2", scratch)
        self.regular = ImageFont.truetype(regular, FONT_SIZE)
        self.bold = ImageFont.truetype(bold, FONT_SIZE)
        sans = plex("plex-sans-latin-wght.woff2", scratch)
        self.sans = ImageFont.truetype(sans, 13)
        self.tagline = ImageFont.truetype(sans, 17)
        self.cmap = set(TTFont(FONTS / "ibm-plex-mono-latin-400.woff2").getBestCmap())
        ascent, descent = self.regular.getmetrics()
        self.offset = (CELL_H - ascent - descent) // 2
        self.fallbacks: dict[str, ImageFont.FreeTypeFont] = {}
        self.lockup = lockup(scratch, width=360)
        self.base = self.window(lockup(scratch, height=14))

    def render(self, frame: Frame) -> Image.Image:
        image = self.base.copy()
        draw = ImageDraw.Draw(image)
        if frame.card:
            self.title_card(image, draw, outro=frame.card == "outro")
            return image
        for y, row in enumerate(frame.rows):
            self.row(draw, y, row)
        if frame.cursor is not None:
            self.cursor(draw, frame, *frame.cursor)
        return image

    def window(self, mark: Image.Image) -> Image.Image:
        """The backdrop, the shadow and the empty window, drawn once."""
        image = backdrop()
        box = (MARGIN_X, MARGIN_Y, MARGIN_X + WIN_W - 1, MARGIN_Y + WIN_H - 1)
        shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        drop = (box[0], box[1] + 18, box[2], box[3] + 18)
        ImageDraw.Draw(shadow).rounded_rectangle(drop, RADIUS, fill=(0, 0, 0, 150))
        image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18)))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(box, RADIUS, fill=COLORS["bg"], outline=COLORS["border"])
        bar = (box[0], box[1], box[2], box[1] + BAR)
        draw.rounded_rectangle(
            bar,
            RADIUS,
            fill=COLORS["bar"],
            outline=COLORS["border"],
            corners=(True, True, False, False),
        )
        draw.line((box[0], bar[3], box[2], bar[3]), fill=COLORS["border"])
        self.title_bar(image, draw, mark)
        return image.convert("RGB")

    def title_bar(
        self, image: Image.Image, draw: ImageDraw.ImageDraw, mark: Image.Image
    ) -> None:
        middle = MARGIN_Y + BAR // 2
        for index, color in enumerate(LIGHTS):
            x = MARGIN_X + 20 + 20 * index
            image.alpha_composite(light(color), (x - 6, middle - 6))
        draw.text(
            (WIDTH / 2, middle),
            TITLE,
            font=self.sans,
            fill=COLORS["title"],
            anchor="mm",
        )
        faint = mark.copy()
        faint.putalpha(faint.getchannel("A").point(lambda alpha: alpha * 35 // 100))
        left = MARGIN_X + WIN_W - 16 - faint.width
        image.alpha_composite(faint, (left, middle - faint.height // 2))

    def title_card(
        self, image: Image.Image, draw: ImageDraw.ImageDraw, *, outro: bool
    ) -> None:
        """The lockup and the tagline, centred where the terminal would be."""
        centre_x = MARGIN_X + WIN_W // 2
        centre_y = PAD_Y + ROWS * CELL_H // 2 - 30
        logo = self.lockup
        image.paste(
            logo, (centre_x - logo.width // 2, centre_y - logo.height // 2), logo
        )
        below = centre_y + logo.height // 2 + 34
        draw.text(
            (centre_x, below),
            TAGLINE,
            font=self.tagline,
            fill=COLORS["title"],
            anchor="mm",
        )
        if outro:
            draw.text(
                (centre_x, below + 34),
                SITE,
                font=self.sans,
                fill=COLORS["prompt"],
                anchor="mm",
            )

    def row(self, draw: ImageDraw.ImageDraw, y: int, row: Row) -> None:
        for x, item in enumerate(row):
            front, back = cell_colors(item)
            if back != COLORS["bg"]:
                left, top = PAD_X + x * CELL_W, PAD_Y + y * CELL_H
                draw.rectangle(
                    (left, top, left + CELL_W - 1, top + CELL_H - 1), fill=back
                )
            self.glyph(draw, x, y, item, front)

    def glyph(
        self, draw: ImageDraw.ImageDraw, x: int, y: int, item: Cell, color: str
    ) -> None:
        text = item[0]
        if not text.strip():
            return
        left, top = PAD_X + x * CELL_W, PAD_Y + y * CELL_H
        if draw_shape(draw, text, left, top, color):
            return
        if ord(text[0]) in self.cmap:
            font = self.bold if item[3] else self.regular
            draw.text((left, top + self.offset), text, font=font, fill=color)
            return
        font = self.fallback(text[0])
        center = (left + CELL_W / 2, top + CELL_H / 2)
        draw.text(center, text, font=font, fill=color, anchor="mm")

    def fallback(self, char: str) -> ImageFont.FreeTypeFont:
        if char not in self.fallbacks:
            path = fallback_font(char)
            print(f"fallback U+{ord(char):04X} {char} -> {path.name}")
            self.fallbacks[char] = ImageFont.truetype(path, FONT_SIZE)
        return self.fallbacks[char]

    def cursor(self, draw: ImageDraw.ImageDraw, frame: Frame, x: int, y: int) -> None:
        if not (0 <= x < COLS and 0 <= y < ROWS):
            return
        left, top = PAD_X + x * CELL_W, PAD_Y + y * CELL_H
        draw.rectangle(
            (left, top + 2, left + CELL_W - 1, top + CELL_H - 3), fill=COLORS["cursor"]
        )
        item = frame.rows[y][x]
        self.glyph(draw, x, y, item, COLORS["bg"])


# Box-drawing lines: (up, down, left, right) from the centre of the cell.
BOX = {
    "\u2502": (1, 1, 0, 0),
    "\u2500": (0, 0, 1, 1),
    "\u250c": (0, 1, 0, 1),
    "\u2510": (0, 1, 1, 0),
    "\u2514": (1, 0, 0, 1),
    "\u2518": (1, 0, 1, 0),
    "\u251c": (1, 1, 0, 1),
    "\u2524": (1, 1, 1, 0),
    "\u252c": (0, 1, 1, 1),
    "\u2534": (1, 0, 1, 1),
    "\u253c": (1, 1, 1, 1),
}
SHAPES = (
    "\u25c6\u25c7\u25cf\u25cb\u25a0\u25b2\u25e2\u25e4\u2714\u2718\u2190\u2192\u276f"
)


def draw_shape(
    draw: ImageDraw.ImageDraw, text: str, left: int, top: int, color: str
) -> bool:
    """Draw lines, geometric shapes and braille spinners crisply, at cell size."""
    char = text[0]
    cx, cy = left + CELL_W // 2, top + CELL_H // 2
    if char in BOX:
        up, down, west, east = BOX[char]
        draw.line(
            (cx, top if up else cy, cx, top + CELL_H - 1 if down else cy), fill=color
        )
        draw.line(
            (left if west else cx, cy, left + CELL_W - 1 if east else cx, cy),
            fill=color,
        )
        return True
    if ord(char) in BRAILLE:
        braille(draw, ord(char) - BRAILLE.start, cx, cy, color)
        return True
    if char in SHAPES:
        shape(draw, char, cx, cy, color)
        return True
    return False


def braille(draw: ImageDraw.ImageDraw, bits: int, cx: int, cy: int, color: str) -> None:
    dots = ((0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2), (0, 3), (1, 3))
    for bit, (column, line) in enumerate(dots):
        if bits & (1 << bit):
            x, y = cx - 2 + 4 * column, cy - 6 + 4 * line
            draw.ellipse((x - 1, y - 1, x + 1, y + 1), fill=color)


def strokes(char: str, cx: int, cy: int) -> list[tuple[str, Any]]:
    """The pen strokes of a geometric glyph centred on ``(cx, cy)``."""
    diamond = [(cx, cy - 5), (cx + 5, cy), (cx, cy + 5), (cx - 5, cy)]
    circle = (cx - 4, cy - 4, cx + 4, cy + 4)
    square = [(cx - 4, cy - 4), (cx + 4, cy - 4), (cx + 4, cy + 4), (cx - 4, cy + 4)]
    return {
        "\u25c6": [("fill", diamond)],
        "\u25c7": [("outline", diamond)],
        "\u25cf": [("dot", circle)],
        "\u25cb": [("ring", circle)],
        "\u25a0": [("fill", square)],
        "\u25b2": [("fill", [(cx, cy - 5), (cx + 5, cy + 4), (cx - 5, cy + 4)])],
        "\u25e2": [("fill", [(cx + 4, cy - 7), (cx + 4, cy + 6), (cx - 5, cy + 6)])],
        "\u25e4": [("fill", [(cx - 5, cy - 7), (cx + 4, cy - 7), (cx - 5, cy + 6)])],
        "\u2714": [("line", [(cx - 4, cy), (cx - 1, cy + 4), (cx + 5, cy - 5)])],
        "\u2718": [
            ("line", [(cx - 4, cy - 4), (cx + 4, cy + 4)]),
            ("line", [(cx - 4, cy + 4), (cx + 4, cy - 4)]),
        ],
        "\u2190": [
            ("thin", [(cx - 4, cy), (cx + 4, cy)]),
            ("fill", [(cx - 4, cy), (cx - 1, cy - 3), (cx - 1, cy + 3)]),
        ],
        "\u2192": [
            ("thin", [(cx - 4, cy), (cx + 4, cy)]),
            ("fill", [(cx + 4, cy), (cx + 1, cy - 3), (cx + 1, cy + 3)]),
        ],
        "\u276f": [("line", [(cx - 2, cy - 5), (cx + 3, cy), (cx - 2, cy + 5)])],
    }[char]


PENS = {
    "fill": lambda draw, points, color: draw.polygon(points, fill=color),
    "outline": lambda draw, points, color: draw.polygon(points, outline=color),
    "dot": lambda draw, points, color: draw.ellipse(points, fill=color),
    "ring": lambda draw, points, color: draw.ellipse(points, outline=color),
    "line": lambda draw, points, color: draw.line(
        points, fill=color, width=2, joint="curve"
    ),
    "thin": lambda draw, points, color: draw.line(points, fill=color),
}


def shape(draw: ImageDraw.ImageDraw, char: str, cx: int, cy: int, color: str) -> None:
    for pen, points in strokes(char, cx, cy):
        PENS[pen](draw, points, color)


def encode(frames: list[Frame], painter: Painter, scratch: Path) -> Path:
    """Draw every frame and join them with ffmpeg's two-pass palette."""
    listing = []
    for number, frame in enumerate(frames):
        path = scratch / f"f{number:04d}.png"
        painter.render(frame).save(path, compress_level=1)
        listing.append(f"file '{path}'\nduration {frame.duration / 1000:.3f}")
    listing.append(f"file '{scratch / f'f{len(frames) - 1:04d}.png'}'")
    concat = scratch / "frames.txt"
    concat.write_text("\n".join(listing) + "\n")
    still = next(frame for frame in reversed(frames) if not frame.card)
    painter.render(still).save(ASSETS / "demo.png", optimize=True)
    source = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i"]
    palette = scratch / "palette.png"
    subprocess.run(
        [
            *source,
            str(concat),
            "-vf",
            "palettegen=max_colors=128:stats_mode=diff",
            str(palette),
        ],
        check=True,
    )
    gif = ASSETS / "demo.gif"
    subprocess.run(
        [
            *source,
            str(concat),
            "-i",
            str(palette),
            "-lavfi",
            "[0:v][1:v]paletteuse=dither=none:diff_mode=rectangle",
            "-fps_mode",
            "vfr",
            "-loop",
            "0",
            str(gif),
        ],
        check=True,
    )
    return gif


def hinoki_executable(given: str | None) -> Path:
    found = given or shutil.which("hinoki")
    if not found:
        raise DemoError("hinoki is not on the PATH: run uv sync --locked first")
    return Path(found).absolute()


def require_tools() -> None:
    for tool in ("ffmpeg", "fc-match", "rsvg-convert", "git", "uv"):
        if shutil.which(tool) is None:
            raise DemoError(f"{tool} is needed to record the GIF")


def require_free_port() -> None:
    """``hinoki dev`` must show the documented default port, not another one."""
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", PORT))
        except OSError as error:
            raise DemoError(f"port {PORT} is in use: stop the server first") from error


def refuse_paths(frames: list[Frame], scratch: Path) -> None:
    """Fail when a screen shows an absolute path: the scratch or home directory."""
    shown = ["".join(item[0] for item in row) for frame in frames for row in frame.rows]
    for path in (str(scratch), str(Path.home())):
        if any(path in line for line in shown):
            raise DemoError(f"a frame shows the absolute path {path}")


def parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hinoki", help="hinoki executable (default: from PATH)")
    parser.add_argument(
        "--keep-frames", type=Path, help="also copy the PNG frames here"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse(argv)
    require_tools()
    require_free_port()
    env = child_env(hinoki_executable(args.hinoki))
    with tempfile.TemporaryDirectory(prefix="hinoki-demo-") as name:
        scratch = Path(name)
        frames = compress(record(scratch, env))
        refuse_paths(frames, scratch)
        pictures = scratch / "frames"
        pictures.mkdir()
        gif = encode(frames, Painter(scratch), pictures)
        if args.keep_frames:
            shutil.copytree(pictures, args.keep_frames, dirs_exist_ok=True)
    size = gif.stat().st_size
    seconds = sum(frame.duration for frame in frames) / 1000
    print(f"wrote {gif} ({size:,} bytes, {len(frames)} frames, {seconds:.1f} s)")
    if size > MAX_BYTES:
        raise DemoError(f"{gif.name} is larger than {MAX_BYTES:,} bytes")


if __name__ == "__main__":
    main()
