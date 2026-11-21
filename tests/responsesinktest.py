"""`respond_stream`, `respond_file` and `ResponseSink` on the classifier."""

import asyncio
from pathlib import Path

import pytest

from hinoki import Hinoki, core, http
from hinoki.http.request import Request
from hinoki.runtime.servercore import build_server_core

HEADERS = [(b"content-type", b"text/event-stream")]
STREAM_DEPTH = 8


@http.get("/stream")
def stream(request: Request) -> dict[str, str]:
    return {}


def request_and_submission() -> tuple[
    core.ServerCore, core.PreparedRequest, core.Submission
]:
    app = Hinoki()
    app.include(stream)
    server = build_server_core(app.asgi().pipeline)
    server.set_ready(True)
    submission = server.submit("GET", "/stream", b"", [])
    (request,) = server.drain(1)
    return server, request, submission


def test_a_stream_head_has_no_length_and_gives_the_slot_back() -> None:
    server, request, submission = request_and_submission()
    assert server.in_flight == 1
    sink = request.respond_stream(200, HEADERS, [])
    assert sink is not None
    assert server.in_flight == 0
    reply = submission.reply()
    assert reply is not None
    status, headers, body = reply
    assert (status, body) == (200, b"")
    names = {name for name, _ in headers}
    assert b"content-length" not in names
    assert b"x-request-id" in names
    assert request.respond_stream(200, HEADERS, []) is None
    assert request.respond(200, [], b"") is False


def test_a_declared_length_is_sent_as_content_length() -> None:
    _, request, submission = request_and_submission()
    assert request.respond_stream(200, HEADERS, [], length=12) is not None
    reply = submission.reply()
    assert reply is not None
    assert dict(reply[1])[b"content-length"] == b"12"


def test_a_stream_refuses_a_bodyless_status_and_a_bad_timer() -> None:
    _, request, _ = request_and_submission()
    with pytest.raises(ValueError, match="cannot have a body"):
        request.respond_stream(204, [], [])
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="idle_timeout"):
            request.respond_stream(200, [], [], idle_timeout=bad)
    with pytest.raises(ValueError, match="keepalive"):
        request.respond_stream(200, [], [], keepalive=0.0)
    assert (
        request.respond_stream(200, [], [], idle_timeout=5.0, keepalive=0.5) is not None
    )


def test_a_file_head_has_no_length_for_the_connection_to_state(tmp_path: Path) -> None:
    _, request, submission = request_and_submission()
    assert request.respond_file(
        200, [(b"content-type", b"text/plain")], [], tmp_path / "f"
    )
    reply = submission.reply()
    assert reply is not None
    assert b"content-length" not in {name for name, _ in reply[1]}
    _, other, _ = request_and_submission()
    with pytest.raises(ValueError, match="cannot have a body"):
        other.respond_file(304, [], [], tmp_path / "f")


def test_the_sink_takes_depth_pieces_then_refuses_and_still_aborts() -> None:
    _, request, submission = request_and_submission()
    sink = request.respond_stream(200, HEADERS, [])
    assert sink is not None
    accepted = [sink.try_send(b"x") for _ in range(STREAM_DEPTH + 3)]
    assert accepted == [True] * STREAM_DEPTH + [False] * 3
    sink.abort()
    sink.abort()
    sink.finish()
    assert sink.try_send(b"y") is False
    assert submission.reply() is not None


def test_ready_waits_for_room_and_closed_tells_a_client_going_away() -> None:
    async def scenario() -> tuple[bool, bool, bool, bool]:
        _, request, submission = request_and_submission()
        sink = request.respond_stream(200, HEADERS, [])
        assert sink is not None
        room = await sink.ready()
        for _ in range(STREAM_DEPTH):
            sink.try_send(b"x")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(sink.ready(), 0.1)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(sink.closed(), 0.1)
        waiting = asyncio.ensure_future(sink.closed())
        await asyncio.sleep(0.05)
        submission.cancel()
        del submission, request
        gone = await asyncio.wait_for(waiting, 2)
        return room, gone, await sink.ready(), await sink.closed()

    # `cancel` drops the receiver of the reply, and with it the body channel.
    assert asyncio.run(scenario()) == (True, True, False, True)


def test_closed_is_false_once_the_response_was_finished_or_aborted() -> None:
    async def scenario(end: str) -> bool:
        _, request, submission = request_and_submission()
        sink = request.respond_stream(200, HEADERS, [])
        assert sink is not None
        waiting = asyncio.ensure_future(sink.closed())
        await asyncio.sleep(0.05)
        getattr(sink, end)()
        outcome = await asyncio.wait_for(waiting, 2)
        assert submission.reply() is not None
        return outcome

    assert asyncio.run(scenario("finish")) is False
    assert asyncio.run(scenario("abort")) is False


def test_sse_frame_is_wire_text_and_refuses_a_line_break_in_a_name() -> None:
    assert core.sse_frame(data="a\nb", event="tick", id="7", retry=1500) == (
        b"event: tick\nid: 7\nretry: 1500\ndata: a\ndata: b\n\n"
    )
    assert core.sse_frame(comment="hi") == b": hi\n\n"
    with pytest.raises(ValueError, match="event"):
        core.sse_frame(data="x", event="a\nb")
    with pytest.raises(ValueError, match="at least one"):
        core.sse_frame()
