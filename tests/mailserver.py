"""A fake SMTP server on 127.0.0.1 for the email tests: scripted replies, a
record of what the client sent, optional STARTTLS or implicit TLS. It never
leaves the machine and accepts only synthetic data."""

from __future__ import annotations

import asyncio
import base64
import ssl
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

TLS_DIRECTORY = Path(__file__).resolve().parent / "tls"
CERTIFICATE = TLS_DIRECTORY / "localhost-cert.pem"
KEY = TLS_DIRECTORY / "localhost-key.pem"
PASSWORD = "synthetic-pw-4d7e1c"
STALL = "<stall>"  # never answer this step
CLOSE = "<close>"  # drop the connection instead of answering
TERMINATOR = b"\r\n.\r\n"


def server_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(CERTIFICATE, KEY)
    return context


@dataclass(frozen=True, slots=True)
class Received:
    sender: str
    recipients: tuple[str, ...]
    data: bytes


class FakeSMTP:
    """``script["RCPT"] = ["550 5.1.1 no", ...]`` answers the next RCPT commands
    with those replies (then the normal ones); the keys are GREETING, EHLO, AUTH,
    MAIL, RCPT, DATA and END (the reply to the message terminator)."""

    def __init__(
        self,
        *,
        tls: bool = False,
        implicit_tls: bool = False,
        credentials: tuple[str, str] | None = ("user", PASSWORD),
        size: int | None = None,
        mechanisms: str = "PLAIN LOGIN",
    ) -> None:
        self.context = server_context() if tls or implicit_tls else None
        self.implicit_tls = implicit_tls
        self.credentials = credentials
        self.size = size
        self.mechanisms = mechanisms
        self.script: defaultdict[str, deque[str]] = defaultdict(deque)
        self.commands: list[str] = []
        self.messages: list[Received] = []
        self.connections = 0
        self.port = 0
        self._release = asyncio.Event()
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def __aenter__(self) -> FakeSMTP:
        ssl_context = self.context if self.implicit_tls else None
        self._server = await asyncio.start_server(
            self._serve, "127.0.0.1", 0, ssl=ssl_context
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self._release.set()
        for writer in list(self._writers):
            writer.close()
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    def saw(self, verb: str) -> int:
        return sum(1 for line in self.commands if line.split(" ")[0] == verb)

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        self._writers.add(writer)
        try:
            if await self._say(writer, "GREETING", "220 fake.invalid ESMTP"):
                await self._session(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()

    async def _say(self, writer: asyncio.StreamWriter, step: str, normal: str) -> bool:
        queue = self.script[step]
        reply = queue.popleft() if queue else normal
        if reply == STALL:
            await self._release.wait()
            return False
        if reply == CLOSE:
            return False
        writer.write(reply.replace("\n", "\r\n").encode() + b"\r\n")
        await writer.drain()
        return True

    async def _session(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        state = _Session()
        while True:
            line = (await reader.readline()).decode("ascii", "replace").rstrip("\r\n")
            if not line:
                return
            verb = line.split(" ")[0].upper()
            self.commands.append("AUTH" if verb == "AUTH" else line)
            if verb == "QUIT":
                await self._say(writer, "QUIT", "221 bye")
                return
            if not await self._command(verb, line, reader, writer, state):
                return

    async def _command(
        self,
        verb: str,
        line: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        state: _Session,
    ) -> bool:
        match verb:
            case "EHLO" | "HELO":
                return await self._say(writer, "EHLO", self._ehlo(state))
            case "STARTTLS":
                if not await self._say(writer, "STARTTLS", "220 ready"):
                    return False
                assert self.context is not None
                await writer.start_tls(self.context)
                state.secure = True
                return True
            case "AUTH":
                return await self._authenticate(line, reader, writer)
            case "MAIL":
                state.sender = line.split("<", 1)[1].split(">", 1)[0]
                state.recipients = []
                return await self._say(writer, "MAIL", "250 2.1.0 ok")
            case "RCPT":
                address = line.split("<", 1)[1].split(">", 1)[0]
                reply = await self._say(writer, "RCPT", "250 2.1.5 ok")
                state.recipients.append(address)
                return reply
            case "DATA":
                return await self._data(reader, writer, state)
            case "RSET":
                return await self._say(writer, "RSET", "250 2.0.0 ok")
            case _:
                return await self._say(writer, "OTHER", "502 5.5.1 unknown")

    def _ehlo(self, state: _Session) -> str:
        lines = ["250-fake.invalid"]
        if self.context is not None and not state.secure and not self.implicit_tls:
            lines.append("250-STARTTLS")
        if self.credentials is not None:
            lines.append(f"250-AUTH {self.mechanisms}")
        if self.size is not None:
            lines.append(f"250-SIZE {self.size}")
        lines.append("250 8BITMIME")
        return "\n".join(lines)

    async def _authenticate(
        self,
        line: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> bool:
        parts = line.split(" ")
        if parts[1].upper() == "PLAIN":
            _, user, password = base64.b64decode(parts[2]).split(b"\0")
            given = (user.decode(), password.decode())
        else:
            writer.write(b"334 VXNlcm5hbWU6\r\n")
            user = base64.b64decode((await reader.readline()).strip())
            writer.write(b"334 UGFzc3dvcmQ6\r\n")
            password = base64.b64decode((await reader.readline()).strip())
            given = (user.decode(), password.decode())
        accepted = given == self.credentials
        return await self._say(
            writer,
            "AUTH",
            "235 2.7.0 welcome" if accepted else "535 5.7.8 rejected",
        )

    async def _data(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        state: _Session,
    ) -> bool:
        if not await self._say(writer, "DATA", "354 go"):
            return False
        body = await reader.readuntil(TERMINATOR)
        text = (b"\r\n" + body[: -len(TERMINATOR)]).replace(b"\r\n..", b"\r\n.")
        self.messages.append(
            Received(state.sender, tuple(state.recipients), text[2:] + b"\r\n")
        )
        return await self._say(writer, "END", "250 2.0.0 queued")


class _Session:
    def __init__(self) -> None:
        self.secure = False
        self.sender = ""
        self.recipients: list[str] = []
