"""Record ``assets/readme/demo.apng`` and ``demo.png`` from a real terminal session.

Run from the repository root after ``uv sync --locked``, with port 8000 free::

    uv run --with pyte==0.8.2 --with pillow==12.0.0 --with fonttools==4.60.1 \\
        --with brotli==1.1.0 python scripts/readme_demo.py

The script runs the real ``hinoki`` CLI in pseudo-terminals inside a throw-away
directory and records one story: the interactive ``hinoki new blog`` (answered
with key presses, down to its last question, "Create an app now?", and the name
``posts``), ``hinoki check`` passing, ``hinoki dev`` serving the project on Kiso
with its docs URL and ``curl`` creating a post through it. A terminal
emulator (pyte) keeps the screen; every frame of the animation is that screen drawn
with IBM Plex Mono from ``theme/static/fonts`` inside a drawn window, between
title cards made from ``lockup-dark.svg``. Glyphs Plex Mono lacks (the ``◢◤``
mark, box lines and corners, spinners) are drawn as shapes.

The children run as in a colour terminal; ``hinoki new`` draws its banner as a
branch of text, so nothing needs to be replaced on the screen.

Only the timing and the motion are edited, never the text: long waits are
shortened, repeated spinner frames are dropped, lines a program prints at once
appear over a few frames, the screen scrolls smoothly instead of jumping and the
cursor blinks while the terminal waits. Re-run the script after a prompt or a
message changes.

There is no shell: the script draws the prompt and the typing, and carries out
``cd`` and the ``&`` that leaves ``hinoki dev`` serving (a shell's job notice is
not drawn). It writes the posts app's files (access policy, model and
endpoint) after ``hinoki new``, and plans and applies the migration of the posts
table off screen,
because ``hinoki migrations`` prints the absolute path of the throw-away
database.

Frames are timed on a 1/60 s grid and written as an APNG, which keeps exact
frame delays: a GIF's delays are whole hundredths of a second and browsers play
those under two hundredths at a tenth of a second, so a GIF cannot reach 60 fps.
The picture is 1992x1280 (the layout is drawn at twice its design size, ``S``).
A frame that does not change the picture is merged into the
previous one, so the file holds only distinct frames.

The children get ``BLOG_ALLOWED_HOSTS`` (a documented setting) so ``hinoki
dev`` does not log the ``allowed_hosts is '*'`` warning meant for deployments,
and ``hinoki new`` skips ``uv sync``: the commands run with this checkout's
``hinoki``.

It needs ``ffmpeg``, ``fc-match``, ``rsvg-convert``, ``git`` and
``curl`` on the PATH.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import math
import os
import random
import re
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
from dataclasses import dataclass, field, replace
from itertools import pairwise
from pathlib import Path
from typing import Any

import pyte
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets" / "readme"
FONTS = ROOT / "theme" / "static" / "fonts"
MAX_BYTES = 12_000_000
PORT = 8000
DOCS = f"http://localhost:{PORT}/docs"

COLS, ROWS = 100, 24
S = 2  # pixels per design unit: the 996x640 layout drawn at 1992x1280 (HD)
CELL_W, CELL_H, FONT_SIZE = 9 * S, 22 * S, 15 * S
BAR, INSET_X, INSET_Y = 36 * S, 18 * S, 14 * S  # title bar; padding in the window
WIN_W = COLS * CELL_W + 2 * INSET_X
WIN_H = BAR + ROWS * CELL_H + 2 * INSET_Y
MARGIN_X, MARGIN_Y, RADIUS = 30 * S, 24 * S, 12 * S  # backdrop around the window
WIDTH, HEIGHT = WIN_W + 2 * MARGIN_X, WIN_H + 2 * MARGIN_Y
PAD_X = MARGIN_X + INSET_X
PAD_Y = MARGIN_Y + BAR + INSET_Y
TITLE = "hinoki \u2014 blog"
TAGLINE = "Typed Python APIs. Architecture rules you can run."
SITE = "hinoki.caspel.dev"
CARD_MS = 1600  # the title card before and after the session

ENTER, DOWN = "\r", "\x1b[B"
SGR_256 = 5  # 38;5;n selects an indexed colour, 38;2;r;g;b a true colour
BRAILLE = range(0x2800, 0x2900)
FPS = 60
TICK = 1000 / FPS  # ms: one frame at 60 fps
PACE = 1.3  # keystroke and pause scale: a little slower than the first recording
INTRO_MS, OUTRO_MS = 1400, 700  # the title card that opens and closes the loop
FADE_MS = 200  # cross-fade between the title card and the terminal
BLINK_MS = 500  # the cursor's on and off phases while the terminal waits
OUTPUT_CAP_MS = 400  # longest pause kept between two outputs of a program
JOIN_MS = 100  # scrolls this close together glide as one
SPIN_MS = 80  # per kept frame of a long spinner
SPIN_FRAMES = 32  # frames kept of a run where one line animates (the shimmer fits)
SETTLE_S = 0.35  # quiet time before a key is pressed
TYPING_SEED = 26  # the typing rhythm is irregular but the same on every run
HESITATE = 0.05  # chance that a typed character is followed by a pause
LONG_LINE = 40  # a typed command longer than this is typed faster
REVEAL_LINES = 3  # lines printed at once that appear one after another

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

# Not ``journal``: systemd's JOURNAL_STREAM would read as an unknown setting.
PROJECT, APP = "blog", "posts"
ACCESS = '''"""Who may write posts."""

from hinoki import Access, BearerAuth, Caller, Permission


class PostPermission(Permission):
    WRITE = "posts:write"


class Tokens(BearerAuth[str]):
    async def resolve_token(self, token: str) -> Caller[str] | None:
        if token != "demo-token":
            return None
        return Caller(id="ann", user="ann", permissions={PostPermission.WRITE})


editors = Access(auth=Tokens(), permissions=[PostPermission.WRITE])
'''

MODEL = '''"""Models owned by this app."""

from uuid import UUID, uuid4

from hinoki.orm import Model, field, pk


class Post(Model):
    id: UUID = pk(default_factory=uuid4)
    title: str = field(max_length=80)
    body: str = ""
'''

API = f"""from typing import Annotated

from hinoki import Field, RouteGroup, Schema, derive

from {PROJECT}.apps.posts.access import editors
from {PROJECT}.apps.posts.models import Post


class PostIn(Schema):
    title: Annotated[str, Field(min_length=1, max_length=80)]


@derive(Post)
class PostOut(Schema): ...


posts = RouteGroup(access=editors)


@posts.post("/", status=201)
async def create_post(data: PostIn) -> PostOut:
    return PostOut.from_model(await Post.acreate(data))
"""

ROUTES = f'''"""Routes of the posts app, mounted at /posts in {PROJECT}/routes.py."""

from {PROJECT}.apps.posts.api import posts

routes = [posts]
'''

PROJECT_ROUTES = f'''"""The project's route map."""

from hinoki import Includable, Mount

from {PROJECT}.api import health
from {PROJECT}.apps.{APP}.routes import routes as posts_routes

routes: list[Includable] = [health, Mount("/{APP}", posts_routes)]
'''
APP_DIR = f"{PROJECT}/apps/{APP}"
TOKEN = "demo-token"  # noqa: S105 - accepted only by the throw-away app
POST = '{"title":"Ship it"}'

Cell = tuple[str, str, str, bool, bool, bool]  # text, fg, bg, bold, reverse, faint
Row = tuple[Cell, ...]


class DemoError(RuntimeError):
    """The session did not go as recorded; the animation would be misleading."""


@dataclass(frozen=True)
class Key:
    """A key press, sent once the screen is quiet (and shows ``wait_for``)."""

    data: str
    pause_ms: int = 600  # how long the animation shows the screen before the key
    wait_for: str = ""
    settle_s: float = SETTLE_S  # real quiet time before the key is sent


@dataclass
class Scene:
    """One command run in the terminal."""

    argv: list[str]
    keys: list[Key] = field(default_factory=list)
    hold_ms: int = 900
    status: int | None = 0  # expected exit status
    timeout_s: float = 120.0
    until: str = ""  # leave it running once the screen shows this (``&``)
    settle_s: float = SETTLE_S  # quiet time after the output before moving on
    shown: str = ""  # the command line to draw, when it differs from ``argv``

    @property
    def command(self) -> str:
        return (self.shown or shlex.join(self.argv)) + (" &" if self.until else "")


@dataclass
class Frame:
    rows: tuple[Row, ...]
    cursor: tuple[int, int] | None
    duration: float  # ms
    pinned: bool = False  # typed or held: its duration is not the real one
    card: bool = False  # the title card instead of the screen
    group: int = 0  # the frames of one burst of output share a group
    strip: tuple[Row, ...] = ()  # rows a smooth scroll moves through
    offset: int = 0  # pixels of ``strip`` scrolled past the top of the screen
    fade: tuple[Frame, Frame, float] | None = None  # from, to and how far


@dataclass
class Job:
    """A command left running with ``&``."""

    process: subprocess.Popen[bytes]
    master: int


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


def read_master(master: int) -> bytes | None:
    """Bytes a program wrote to its terminal, ``None`` once it is closed."""
    try:
        return os.read(master, 65536) or None
    except OSError:
        return None


def spawn(argv: list[str], cwd: Path, env: dict[str, str]) -> Job:
    """Start ``argv`` in a fresh pty sized like the screen."""
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        preexec_fn=take_terminal,  # noqa: PLW1509 - single-threaded; the child needs a controlling tty
    )
    os.close(slave)
    return Job(process, master)


def lines(data: bytes) -> list[bytes]:
    """``data`` cut after each newline, so every printed line gets a frame."""
    return [piece for piece in re.split(b"(?<=\n)", data) if piece]


def keystroke(rng: random.Random, text: str, index: int) -> int:
    """How long a typed character stays before the next one: an irregular
    rhythm, a little slower after a space and with an occasional hesitation."""
    delay = rng.choice((30, 40, 50, 50, 60, 70))
    if text[index] in " /.-":
        delay += 30
    if rng.random() < HESITATE:
        delay += rng.choice((140, 200, 260))
    return round(delay * PACE)


class Session:
    """A terminal screen shared by every scene, recorded as frames."""

    def __init__(self, env: dict[str, str]) -> None:
        self.env = env
        self.screen = DemoScreen(COLS, ROWS)
        self.stream = pyte.ByteStream(self.screen)
        self.frames: list[Frame] = []
        self.rows: dict[Row, Row] = {}
        self.prompted = False
        self.group = 0
        self.clock = time.monotonic()  # when the program last wrote
        self.rng = random.Random(TYPING_SEED)  # noqa: S311 - a typing rhythm, not a secret
        self.jobs: list[Job] = []

    def text(self) -> str:
        return "\n".join(self.screen.display)

    def emit(self, data: str | bytes, duration: float, *, pinned: bool = False) -> None:
        """Feed ``data`` line by line: one group of frames, the last lasting
        ``duration`` and the others none until the motion is timed."""
        self.group += 1
        pieces = lines(data.encode() if isinstance(data, str) else data)
        for index, piece in enumerate(pieces):
            self.stream.feed(piece)
            last = index == len(pieces) - 1
            self.snap(duration if last else 0, pinned=pinned and last)

    def output(self, data: bytes) -> None:
        """A program wrote ``data``: the screen before it lasted as long as the
        program took, up to ``OUTPUT_CAP_MS``."""
        now = time.monotonic()
        last = self.frames[-1]
        if not last.pinned:
            waited = (now - self.clock) * 1000
            last.duration = min(max(waited, TICK), OUTPUT_CAP_MS)
        self.clock = now
        self.emit(data, TICK)

    def snapshot(self) -> tuple[tuple[Row, ...], tuple[int, int] | None]:
        rows = []
        for y in range(ROWS):
            line = self.screen.buffer[y]
            row = tuple(cell(line[x]) for x in range(COLS))
            rows.append(self.rows.setdefault(row, row))
        cursor = self.screen.cursor
        return tuple(rows), None if cursor.hidden else (cursor.x, cursor.y)

    def snap(self, duration: float, *, pinned: bool = False) -> None:
        rows, cursor = self.snapshot()
        last = self.frames[-1] if self.frames else None
        if last and not last.card and last.rows == rows and last.cursor == cursor:
            last.duration += duration
            last.pinned = last.pinned or pinned
            return
        self.frames.append(Frame(rows, cursor, duration, pinned, group=self.group))

    def card(self, duration: int) -> None:
        """Show the title card (drawn by the painter, not terminal output)."""
        rows, _ = self.snapshot()
        self.frames.append(Frame(rows, None, duration, pinned=True, card=True))

    def hold(self, duration: int) -> None:
        """Show the current screen for at least ``duration`` and keep the frame."""
        self.snap(0)
        self.frames[-1].duration = max(self.frames[-1].duration, duration)
        self.frames[-1].pinned = True

    def prompt(self) -> None:
        """Draw the shell prompt on a fresh line, as a shell does after a command."""
        if self.prompted:
            return
        fresh = "\r\n" if self.screen.cursor.x else ""
        self.emit(f"{fresh}{sgr(COLORS['prompt'])}${RESET} ", TICK)
        self.prompted = True

    def type_line(self, text: str, *, think_ms: int = 480) -> None:
        """Type ``text`` at the prompt (there is no shell: the line is drawn)."""
        self.prompt()
        self.hold(think_ms)
        # A long command is typed a little faster, as a practised hand would.
        speed = 0.75 if len(text) > LONG_LINE else 1.0
        for index, char in enumerate(text):
            delay = round(keystroke(self.rng, text, index) * speed)
            self.emit(char, max(delay, TICK), pinned=True)
        self.emit("\r\n", TICK)
        self.prompted = False
        self.clock = time.monotonic()

    def keys(self, text: str, pause_ms: int) -> list[Key]:
        """Key presses that type ``text`` into a program's prompt."""
        delays = [keystroke(self.rng, text, index) for index in range(len(text))]
        return [
            Key(char, pause)
            for char, pause in zip(text, [pause_ms, *delays[:-1]], strict=True)
        ]

    def run(self, scene: Scene, cwd: Path) -> None:
        """Type and run ``scene``; leave it running when it ends with ``&``."""
        self.type_line(scene.command)
        job = spawn(scene.argv, cwd, self.env)
        self.clock = time.monotonic()
        try:
            Pump(self, job, scene).run()
        except BaseException:
            stop_job(job)
            raise
        if scene.until:
            self.jobs.append(job)
        else:
            stop_job(job)
            check_status(scene, job.process.returncode, self.text())
        self.prompt()
        self.hold(scene.hold_ms)

    def stop(self) -> None:
        """Stop the commands left running with ``&``."""
        while self.jobs:
            stop_job(self.jobs.pop())


class Pump:
    """Copy programs' output to the screen, take frames and press keys."""

    def __init__(self, session: Session, job: Job, scene: Scene) -> None:
        self.session = session
        self.job = job
        self.scene = scene
        self.keys = list(scene.keys)
        self.open = [job.master, *(other.master for other in session.jobs)]
        now = time.monotonic()
        self.deadline = now + scene.timeout_s
        self.last_data = now

    def run(self) -> None:
        while self.step():
            if time.monotonic() > self.deadline:
                raise DemoError(f"{self.scene.command} timed out")

    def step(self) -> bool:
        """Handle one read; ``False`` once the scene is over."""
        ready, _, _ = select.select(self.open, [], [], 0.02)
        for master in ready:
            data = read_master(master)
            if data is None:
                self.open.remove(master)
                continue
            self.session.output(data)
            self.last_data = time.monotonic()
        quiet = time.monotonic() - self.last_data
        if self.keys and self.ready(self.keys[0], quiet):
            self.press(self.keys.pop(0))
        return not self.over(quiet)

    def over(self, quiet: float) -> bool:
        if quiet < self.scene.settle_s or self.keys:
            return False
        if self.scene.until:
            return self.scene.until in self.session.text()
        return self.job.process.poll() is not None

    def ready(self, key: Key, quiet: float) -> bool:
        if quiet < key.settle_s:
            return False
        return not key.wait_for or key.wait_for in self.session.text()

    def press(self, key: Key) -> None:
        self.session.hold(round(key.pause_ms * PACE))
        os.write(self.job.master, key.data.encode())
        self.last_data = self.session.clock = time.monotonic()


def stop_job(job: Job) -> None:
    """Stop whatever the command left running in its session (server workers)."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(job.process.pid, signal.SIGTERM)
    try:
        job.process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(job.process.pid, signal.SIGKILL)
        job.process.wait()
    os.close(job.master)


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
        f"{PROJECT.upper()}_ALLOWED_HOSTS": "127.0.0.1,localhost",
        "PATH": path,
    }


def new_keys(session: Session) -> list[Key]:
    """Answers to ``hinoki new``: ``blog``, SQLite, no ``uv sync``, app ``posts``."""
    typed = session.keys(PROJECT, 900)
    # The banner image arrives before the prompt reads keys: wait for the prompt.
    typed[0] = replace(typed[0], wait_for="Project name")
    return [
        *typed,
        Key(ENTER, 400),  # name
        Key(ENTER, 600),  # parent directory: .
        Key(DOWN, 800),  # database: none -> sqlite
        Key(ENTER, 450),
        Key(ENTER, 700),  # server: kiso
        *(Key(ENTER, 380) for _ in range(3)),  # agent files, skills, git
        Key("n", 600),  # install: this checkout's hinoki is already on the PATH
        Key(ENTER, 350),
        Key(ENTER, 800, wait_for="Create an app now?"),  # yes: the default
        *replace_first(session.keys(APP, 700), wait_for="App name"),
        Key(ENTER, 500),
    ]


def replace_first(keys: list[Key], **changes: Any) -> list[Key]:
    """``keys`` with ``changes`` applied to the first one."""
    return [replace(keys[0], **changes), *keys[1:]]


def write_app(project: Path) -> None:
    """Fill in the posts app ``hinoki new`` created and mount it."""
    package = project / APP_DIR
    (package / "access.py").write_text(ACCESS)
    (package / "models.py").write_text(MODEL)
    (package / "api.py").write_text(API)
    (package / "routes.py").write_text(ROUTES)
    (project / PROJECT / "routes.py").write_text(PROJECT_ROUTES)


def offscreen(argv: list[str], cwd: Path, env: dict[str, str]) -> None:
    """Run a step the animation does not show; fail the recording if it fails."""
    done = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode:
        output = (done.stdout + done.stderr).rstrip()
        raise DemoError(f"{shlex.join(argv)} exited with {done.returncode}:\n{output}")


def expect(session: Session, *texts: str) -> None:
    """Fail unless the screen shows every one of ``texts``."""
    screen = session.text()
    missing = [text for text in texts if text not in screen]
    if missing:
        raise DemoError(f"the screen does not show {missing}:\n{screen.rstrip()}")


def refuse(session: Session, *texts: str) -> None:
    """Fail if the screen shows any of ``texts``."""
    screen = session.text()
    if any(text in screen for text in texts):
        raise DemoError(f"the screen shows one of {list(texts)}:\n{screen.rstrip()}")


def create_project(session: Session, workdir: Path) -> Path:
    """``hinoki new blog`` with its app, then ``cd blog``."""
    new = Scene(["hinoki", "new"], keys=new_keys(session), hold_ms=1800, timeout_s=600)
    session.run(new, workdir)
    project = workdir / PROJECT
    if not (project / "hinoki.toml").is_file():
        raise DemoError(f"hinoki new did not create {PROJECT}/hinoki.toml")
    expect(session, f"Created app {APP}")
    write_app(project)
    session.type_line(f"cd {PROJECT}")
    return project


def check_rules(session: Session, project: Path) -> None:
    """Show ``hinoki check`` flagging missing ``atomic=True``, then passing."""
    # From here to the request everything fits on one screen: no scrolling.
    session.run(Scene(["clear"], hold_ms=200), project)
    session.run(Scene(["hinoki", "check"], status=1, hold_ms=4200), project)
    expect(session, "HNK-TXN-001", "atomic=True", "1 violated")
    fix_script = (
        f"from pathlib import Path; p = Path('{APP_DIR}/api.py'); "
        "p.write_text(p.read_text().replace("
        "'access=editors)', 'access=editors, atomic=True)'))"
    )
    shown_fix = (
        f"sed -i 's/access=editors)/access=editors, atomic=True)/' {APP_DIR}/api.py"
    )
    session.run(
        Scene(["python3", "-c", fix_script], shown=shown_fix, hold_ms=900),
        project,
    )
    session.run(Scene(["hinoki", "check"], status=0, hold_ms=2000), project)
    expect(session, "0 violated")


def serve(session: Session, project: Path) -> None:
    """Create the posts table, serve the project on Kiso and create a post."""
    offscreen(["hinoki", "migrations", "plan", APP], project, session.env)
    offscreen(["hinoki", "migrations", "apply"], project, session.env)
    # The worker imports the app after the banner: wait until it is quiet.
    dev = Scene(["hinoki", "dev"], hold_ms=700, until="listening on", settle_s=2.5)
    session.run(dev, project)
    refuse(session, "Traceback", "WARNING")
    expect(session, f"docs: http://127.0.0.1:{PORT}/docs")
    curl = Scene(
        [
            "curl",
            "-s",
            f"localhost:{PORT}/{APP}",
            "-H",
            f"authorization: Bearer {TOKEN}",
            "--json",
            POST,
        ],
        hold_ms=2600,
        settle_s=0.8,  # the server logs the request just after curl exits
    )
    session.run(curl, project)
    expect(session, '"title":"Ship it"', " 201 ")
    refuse(session, "Traceback")
    docs = Scene(
        ["curl", "-so", "/dev/null", "-w", "%{http_code} %{url}\\n", DOCS],
        hold_ms=2400,
        settle_s=0.8,
    )
    session.run(docs, project)
    expect(session, f"200 {DOCS}")


def record(workdir: Path, env: dict[str, str]) -> list[Frame]:
    """Run every scene and return the frames, failing on unexpected output."""
    session = Session(env)
    session.card(INTRO_MS)
    try:
        project = create_project(session, workdir)
        check_rules(session, project)
        serve(session, project)
    finally:
        session.stop()
    session.card(OUTRO_MS)
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


def ease_out(t: float) -> float:
    """Cubic ease-out: fast at first, settling at the end."""
    return 1 - (1 - t) ** 3


def ease_in_out(t: float) -> float:
    """Sine ease-in-out: slow at both ends."""
    return (1 - math.cos(math.pi * t)) / 2


def blank(row: Row) -> bool:
    return all(item[0] == " " for item in row)


def matches(before: Frame, after: Frame, k: int) -> bool:
    """Whether ``after`` is ``before`` scrolled up by ``k`` lines (its last line,
    which the program may still have been writing, aside)."""
    overlap = range(ROWS - 1 - k)
    if all(blank(after.rows[i]) for i in overlap):
        return False
    return all(after.rows[i] is before.rows[i + k] for i in overlap)


def scrolled(before: Frame, after: Frame) -> int:
    """How many lines the screen scrolled from ``before`` to ``after``."""
    at_bottom = before.cursor is not None and before.cursor[1] == ROWS - 1
    if before.card or not (at_bottom or not blank(before.rows[-1])):
        return 0
    return next((k for k in range(1, ROWS - 1) if matches(before, after, k)), 0)


def bursts(frames: list[Frame]) -> Iterator[tuple[Frame | None, list[Frame]]]:
    """Each run of frames printed at once, with the frame shown before it."""
    start = 0
    for index in range(1, len(frames) + 1):
        if index < len(frames) and frames[index].group == frames[start].group:
            continue
        yield (frames[start - 1] if start else None), frames[start:index]
        start = index


def scroll_strip(before: Frame, burst: list[Frame]) -> tuple[tuple[Row, ...], int]:
    """The rows the screen moves through and how many lines it scrolls."""
    strip, top, previous = list(before.rows), 0, before
    for frame in burst:
        top += scrolled(previous, frame)
        strip.extend(frame.rows[len(strip) - top :])
        strip[top : top + ROWS] = frame.rows
        previous = frame
    return tuple(strip), top


def scroll(before: Frame, burst: list[Frame], lines: int) -> list[Frame]:
    """Glide the screen up ``lines`` lines instead of jumping."""
    strip, _ = scroll_strip(before, burst)
    last = burst[-1]
    steps = min(max(3 + lines, 4), 9)  # 80 to 180 ms: one line glides quickly
    glide = [
        replace(
            last,
            cursor=None,
            duration=TICK,
            pinned=False,
            strip=strip,
            offset=round(ease_out(step / steps) * lines * CELL_H),
        )
        for step in range(1, steps)
    ]
    return [*glide, last]


def reveal(burst: list[Frame]) -> list[Frame]:
    """Show lines printed at once one after another, quickly then settling."""
    count = len(burst) - 1
    total = min(400, 40 * count)
    times = [total * ease_out(index / count) for index in range(count + 1)]
    for frame, (start, end) in zip(burst, pairwise(times), strict=False):
        frame.duration = max(frame.duration, end - start)
    return burst


def scroll_lines(before: Frame | None, burst: list[Frame]) -> int:
    if before is None:
        return 0
    return sum(
        scrolled(previous, frame) for previous, frame in pairwise([before, *burst])
    )


def joined_bursts(frames: list[Frame]) -> list[tuple[Frame | None, list[Frame], int]]:
    """Bursts with the lines they scroll; scrolling bursts that follow each
    other within ``JOIN_MS`` are joined, so the screen glides once."""
    out: list[tuple[Frame | None, list[Frame], int]] = []
    for before, burst in bursts(frames):
        lines = scroll_lines(before, burst)
        if (
            out
            and lines
            and out[-1][2]
            and before is not None
            and not before.pinned
            and before.duration <= JOIN_MS
        ):
            first, previous, scrolled_before = out[-1]
            out[-1] = (first, [*previous, *burst], scrolled_before + lines)
        else:
            out.append((before, burst, lines))
    return out


def animate(frames: list[Frame]) -> list[Frame]:
    """Give each burst of output its motion: a scroll, a reveal or a frame."""
    out: list[Frame] = []
    for before, burst, lines in joined_bursts(frames):
        if before is not None and lines:
            out.extend(scroll(before, burst, lines))
        elif len(burst) >= REVEAL_LINES:
            out.extend(reveal(burst))
        else:
            for frame in burst[:-1]:
                frame.duration = max(frame.duration, TICK)
            out.extend(burst)
    return out


def blink(frames: list[Frame]) -> list[Frame]:
    """Blink the cursor while the screen waits, as a terminal does."""
    out: list[Frame] = []
    for frame in frames:
        if frame.cursor is None or frame.strip or frame.duration < 1.5 * BLINK_MS:
            out.append(frame)
            continue
        left, shown = frame.duration, True
        while left > 0:
            phase = left if left < 1.5 * BLINK_MS else BLINK_MS
            cursor = frame.cursor if shown else None
            out.append(replace(frame, cursor=cursor, duration=phase))
            left, shown = left - phase, not shown
    return out


def fades(frames: list[Frame]) -> list[Frame]:
    """Cross-fade between the title card and the terminal."""
    out: list[Frame] = [frames[0]]
    steps = round(FADE_MS / TICK)
    for before, after in pairwise(frames):
        if before.card != after.card:
            out.extend(
                replace(
                    after, duration=TICK, fade=(before, after, ease_in_out(i / steps))
                )
                for i in range(1, steps)
            )
        out.append(after)
    return out


def same(one: Frame, two: Frame) -> bool:
    """Whether two frames draw the same picture."""
    keys = ("rows", "cursor", "card", "strip", "offset", "fade")
    return all(getattr(one, key) == getattr(two, key) for key in keys)


def quantise(frames: list[Frame]) -> list[Frame]:
    """Put every frame on the ``TICK`` grid; merge frames that look the same."""
    out: list[Frame] = []
    clock = shown = 0.0
    for frame in frames:
        clock += frame.duration
        end = round(clock / TICK) * TICK
        if end <= shown:
            continue
        if out and same(out[-1], frame):
            out[-1].duration += end - shown
        else:
            out.append(replace(frame, duration=end - shown))
        shown = end
    return out


def timeline(frames: list[Frame]) -> list[Frame]:
    """The recorded frames with their motion, on the 60 fps grid."""
    return quantise(fades(blink(animate(compress(frames)))))


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
    ImageDraw.Draw(orb).ellipse(
        (-120 * S, -160 * S, 300 * S, 220 * S), fill=(122, 85, 196, 60)
    )
    image.alpha_composite(orb.filter(ImageFilter.GaussianBlur(80 * S)))
    return image


def light(color: str, size: int = 12 * S, scale: int = 8) -> Image.Image:
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
        self.sans = ImageFont.truetype(sans, 13 * S)
        self.tagline = ImageFont.truetype(sans, 17 * S)
        self.cmap = set(TTFont(FONTS / "ibm-plex-mono-latin-400.woff2").getBestCmap())
        ascent, descent = self.regular.getmetrics()
        self.offset = (CELL_H - ascent - descent) // 2
        self.fallbacks: dict[str, ImageFont.FreeTypeFont] = {}
        self.lockup = lockup(scratch, width=360 * S)
        self.base = self.window(lockup(scratch, height=14 * S))
        self.strips: dict[tuple[Row, ...], Image.Image] = {}

    def render(self, frame: Frame) -> Image.Image:
        if frame.fade is not None:
            before, after, amount = frame.fade
            return Image.blend(self.render(before), self.render(after), amount)
        image = self.base.copy()
        draw = ImageDraw.Draw(image)
        if frame.card:
            self.title_card(image, draw)
            return image
        if frame.strip:
            top = frame.offset
            strip = self.strip(frame.strip)
            image.paste(
                strip.crop((0, top, strip.width, top + ROWS * CELL_H)), (PAD_X, PAD_Y)
            )
            return image
        for y, row in enumerate(frame.rows):
            self.row(draw, (PAD_X, PAD_Y + y * CELL_H), row)
        if frame.cursor is not None:
            self.cursor(draw, frame, *frame.cursor)
        return image

    def strip(self, rows: tuple[Row, ...]) -> Image.Image:
        """The rows a scroll moves through, drawn once as one tall image."""
        if rows not in self.strips:
            size = (COLS * CELL_W, len(rows) * CELL_H)
            image = Image.new("RGB", size, COLORS["bg"])
            draw = ImageDraw.Draw(image)
            for y, row in enumerate(rows):
                self.row(draw, (0, y * CELL_H), row)
            self.strips = {rows: image}
        return self.strips[rows]

    def window(self, mark: Image.Image) -> Image.Image:
        """The backdrop, the shadow and the empty window, drawn once."""
        image = backdrop()
        box = (MARGIN_X, MARGIN_Y, MARGIN_X + WIN_W - 1, MARGIN_Y + WIN_H - 1)
        shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        drop = (box[0], box[1] + 8 * S, box[2], box[3] + 8 * S)
        ImageDraw.Draw(shadow).rounded_rectangle(drop, RADIUS, fill=(0, 0, 0, 150))
        image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(10 * S)))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            box, RADIUS, fill=COLORS["bg"], outline=COLORS["border"], width=S
        )
        bar = (box[0], box[1], box[2], box[1] + BAR)
        draw.rounded_rectangle(
            bar,
            RADIUS,
            fill=COLORS["bar"],
            outline=COLORS["border"],
            width=S,
            corners=(True, True, False, False),
        )
        draw.line((box[0], bar[3], box[2], bar[3]), fill=COLORS["border"], width=S)
        self.title_bar(image, draw, mark)
        return image.convert("RGB")

    def title_bar(
        self, image: Image.Image, draw: ImageDraw.ImageDraw, mark: Image.Image
    ) -> None:
        middle = MARGIN_Y + BAR // 2
        for index, color in enumerate(LIGHTS):
            x = MARGIN_X + (20 + 20 * index) * S
            image.alpha_composite(light(color), (x - 6 * S, middle - 6 * S))
        draw.text(
            (WIDTH / 2, middle),
            TITLE,
            font=self.sans,
            fill=COLORS["title"],
            anchor="mm",
        )
        faint = mark.copy()
        faint.putalpha(faint.getchannel("A").point(lambda alpha: alpha * 35 // 100))
        left = MARGIN_X + WIN_W - 16 * S - faint.width
        image.alpha_composite(faint, (left, middle - faint.height // 2))

    def title_card(self, image: Image.Image, draw: ImageDraw.ImageDraw) -> None:
        """The lockup, the tagline and the site, centred where the terminal is;
        the same card opens and closes the loop."""
        centre_x = MARGIN_X + WIN_W // 2
        centre_y = PAD_Y + ROWS * CELL_H // 2 - 30 * S
        logo = self.lockup
        image.paste(
            logo, (centre_x - logo.width // 2, centre_y - logo.height // 2), logo
        )
        below = centre_y + logo.height // 2 + 34 * S
        draw.text(
            (centre_x, below),
            TAGLINE,
            font=self.tagline,
            fill=COLORS["title"],
            anchor="mm",
        )
        draw.text(
            (centre_x, below + 34 * S),
            SITE,
            font=self.sans,
            fill=COLORS["prompt"],
            anchor="mm",
        )

    def row(
        self,
        draw: ImageDraw.ImageDraw,
        origin: tuple[int, int],
        row: Row,
    ) -> None:
        left, top = origin
        for x, item in enumerate(row):
            front, back = cell_colors(item)
            cell_left = left + x * CELL_W
            if back != COLORS["bg"]:
                draw.rectangle(
                    (cell_left, top, cell_left + CELL_W - 1, top + CELL_H - 1),
                    fill=back,
                )
            self.glyph(draw, (cell_left, top), item, front)

    def glyph(
        self, draw: ImageDraw.ImageDraw, at: tuple[int, int], item: Cell, color: str
    ) -> None:
        text = item[0]
        if not text.strip():
            return
        left, top = at
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
            (left, top + 2 * S, left + CELL_W - 1, top + CELL_H - 3 * S),
            fill=COLORS["cursor"],
        )
        self.glyph(draw, (left, top), frame.rows[y][x], COLORS["bg"])


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
# Rounded corners: the directions their lines leave in, and the arc's start angle.
ROUND = {
    "\u256d": (1, 1, 180),
    "\u256e": (-1, 1, 270),
    "\u2570": (1, -1, 90),
    "\u256f": (-1, -1, 0),
}
CORNER = 4 * S  # px radius of a rounded corner
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
        box_line(draw, char, left, top, color)
    elif char in ROUND:
        corner(draw, char, left, top, color)
    elif ord(char) in BRAILLE:
        braille(draw, ord(char) - BRAILLE.start, cx, cy, color)
    elif char in SHAPES:
        shape(draw, char, cx, cy, color)
    else:
        return False
    return True


def box_line(
    draw: ImageDraw.ImageDraw, char: str, left: int, top: int, color: str
) -> None:
    """A straight box-drawing line or junction, from the cell's centre."""
    cx, cy = left + CELL_W // 2, top + CELL_H // 2
    up, down, west, east = BOX[char]
    draw.line(
        (cx, top if up else cy, cx, top + CELL_H - 1 if down else cy),
        fill=color,
        width=S,
    )
    draw.line(
        (left if west else cx, cy, left + CELL_W - 1 if east else cx, cy),
        fill=color,
        width=S,
    )


def corner(
    draw: ImageDraw.ImageDraw, char: str, left: int, top: int, color: str
) -> None:
    """A rounded box corner: a quarter circle joining a line to the cell's
    side and one to its top or bottom edge."""
    across, down, start = ROUND[char]
    cx, cy = left + CELL_W // 2, top + CELL_H // 2
    r = CORNER
    side = left + CELL_W - 1 if across > 0 else left
    edge = top + CELL_H - 1 if down > 0 else top
    draw.line((cx + across * r, cy, side, cy), fill=color, width=S)
    draw.line((cx, cy + down * r, cx, edge), fill=color, width=S)
    ox, oy = cx + across * r, cy + down * r
    draw.arc((ox - r, oy - r, ox + r, oy + r), start, start + 90, fill=color, width=S)


def braille(draw: ImageDraw.ImageDraw, bits: int, cx: int, cy: int, color: str) -> None:
    dots = ((0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2), (0, 3), (1, 3))
    for bit, (column, line) in enumerate(dots):
        if bits & (1 << bit):
            x, y = cx + (4 * column - 2) * S, cy + (4 * line - 6) * S
            draw.ellipse((x - S, y - S, x + S, y + S), fill=color)


# Pen strokes of the geometric glyphs, in design units from the cell's centre.
GLYPHS: dict[str, list[tuple[str, list[tuple[int, int]]]]] = {
    "\u25c6": [("fill", [(0, -5), (5, 0), (0, 5), (-5, 0)])],
    "\u25c7": [("outline", [(0, -5), (5, 0), (0, 5), (-5, 0)])],
    "\u25cf": [("dot", [(-4, -4), (4, 4)])],
    "\u25cb": [("ring", [(-4, -4), (4, 4)])],
    "\u25a0": [("fill", [(-4, -4), (4, -4), (4, 4), (-4, 4)])],
    "\u25b2": [("fill", [(0, -5), (5, 4), (-5, 4)])],
    "\u25e2": [("fill", [(4, -7), (4, 6), (-5, 6)])],
    "\u25e4": [("fill", [(-5, -7), (4, -7), (-5, 6)])],
    "\u2714": [("line", [(-4, 0), (-1, 4), (5, -5)])],
    "\u2718": [
        ("line", [(-4, -4), (4, 4)]),
        ("line", [(-4, 4), (4, -4)]),
    ],
    "\u2190": [
        ("thin", [(-4, 0), (4, 0)]),
        ("fill", [(-4, 0), (-1, -3), (-1, 3)]),
    ],
    "\u2192": [
        ("thin", [(-4, 0), (4, 0)]),
        ("fill", [(4, 0), (1, -3), (1, 3)]),
    ],
    "\u276f": [("line", [(-2, -5), (3, 0), (-2, 5)])],
}


def strokes(char: str, cx: int, cy: int) -> list[tuple[str, Any]]:
    """The pen strokes of a geometric glyph centred on ``(cx, cy)``."""
    return [
        (pen, [(cx + x * S, cy + y * S) for x, y in points])
        for pen, points in GLYPHS[char]
    ]


PENS = {
    "fill": lambda draw, points, color: draw.polygon(points, fill=color),
    "outline": lambda draw, points, color: draw.polygon(points, outline=color),
    "dot": lambda draw, points, color: draw.ellipse(points, fill=color),
    "ring": lambda draw, points, color: draw.ellipse(points, outline=color, width=S),
    "line": lambda draw, points, color: draw.line(
        points, fill=color, width=2 * S, joint="curve"
    ),
    "thin": lambda draw, points, color: draw.line(points, fill=color, width=S),
}


def shape(draw: ImageDraw.ImageDraw, char: str, cx: int, cy: int, color: str) -> None:
    for pen, points in strokes(char, cx, cy):
        PENS[pen](draw, points, color)


def encode(frames: list[Frame], painter: Painter, scratch: Path, colors: int) -> Path:
    """Draw every frame and join them, at 60 fps, into an APNG with ffmpeg."""
    listing = []
    for number, frame in enumerate(frames):
        path = scratch / f"f{number:04d}.png"
        painter.render(frame).save(path, compress_level=1)
        ticks = round(frame.duration / TICK)
        # Without the option an image gets a 25 fps time base: 40 ms steps.
        listing.append(
            f"file '{path}'\noption framerate 6000\nduration {ticks / FPS:.6f}"
        )
    concat = scratch / "frames.txt"
    concat.write_text("\n".join(listing) + "\n")
    still = next(frame for frame in reversed(frames) if still_frame(frame))
    painter.render(replace(still, cursor=None)).save(ASSETS / "demo.png", optimize=True)
    source = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i"]
    palette = scratch / "palette.png"
    subprocess.run(
        [
            *source,
            str(concat),
            "-vf",
            f"palettegen=max_colors={colors}:stats_mode=diff",
            str(palette),
        ],
        check=True,
    )
    movie = ASSETS / "demo.apng"
    subprocess.run(
        [
            *source,
            str(concat),
            "-i",
            str(palette),
            "-lavfi",
            "[0:v][1:v]paletteuse=dither=none",
            "-r",
            str(FPS),
            "-fps_mode",
            "cfr",
            "-plays",
            "0",
            "-f",
            "apng",
            str(movie),
        ],
        check=True,
    )
    return movie


def still_frame(frame: Frame) -> bool:
    """A plain terminal frame: the reduced-motion PNG shows the last one."""
    return not (frame.card or frame.strip or frame.fade)


def apng_delays(movie: Path) -> list[float]:
    """The delay of every frame of the written APNG, in ms."""
    with Image.open(movie) as image:
        delays = []
        for index in range(image.n_frames):
            image.seek(index)
            delays.append(float(image.info["duration"]))
    return delays


def report(movie: Path) -> None:
    """Print the APNG's frame rate and size; fail when it is not 60 fps."""
    delays = apng_delays(movie)
    off = sorted({delay for delay in delays if delay < TICK - 1})
    if off:
        raise DemoError(f"{movie.name} has delays under one {FPS} fps tick: {off}")
    size, seconds = movie.stat().st_size, sum(delays) / 1000
    print(
        f"wrote {movie} ({size:,} bytes, {len(delays)} frames, {seconds:.1f} s, "
        f"{FPS} fps)"
    )


def hinoki_executable(given: str | None) -> Path:
    found = given or shutil.which("hinoki")
    if not found:
        raise DemoError("hinoki is not on the PATH: run uv sync --locked first")
    return Path(found).absolute()


def require_tools() -> None:
    for tool in ("ffmpeg", "fc-match", "rsvg-convert", "git", "sed", "curl"):
        if shutil.which(tool) is None:
            raise DemoError(f"{tool} is needed to record the animation")


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
    parser.add_argument(
        "--colors", type=int, default=128, help="palette size (default: 128)"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse(argv)
    require_tools()
    require_free_port()
    env = child_env(hinoki_executable(args.hinoki))
    with tempfile.TemporaryDirectory(prefix="hinoki-demo-") as name:
        scratch = Path(name)
        frames = timeline(record(scratch, env))
        refuse_paths(frames, scratch)
        pictures = scratch / "frames"
        pictures.mkdir()
        movie = encode(frames, Painter(scratch), pictures, args.colors)
        if args.keep_frames:
            shutil.copytree(pictures, args.keep_frames, dirs_exist_ok=True)
    report(movie)
    if movie.stat().st_size > MAX_BYTES:
        raise DemoError(f"{movie.name} is larger than {MAX_BYTES:,} bytes")


if __name__ == "__main__":
    main()
