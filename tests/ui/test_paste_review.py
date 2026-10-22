"""Review Paste sheet and generation-bound confirm.

The declarative sheet shows the exact preview plus line/character
counts; the session binds the writer generation at open and revalidates
at confirm through the sole current writer (control-lease text input
path). Cancel sends nothing, stale generations fail closed, bracketed
framing follows the engine mode captured at open, and no path
synthesizes Enter. The blocked-composer no-framing exception belongs to and is out of scope here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from relaydeck.client.features.sync.transport import TerminalSocket, TransportError
from relaydeck.client.features.terminal.controller import TerminalController
from relaydeck.client.features.terminal.paste_policy import (
    BRACKET_END,
    BRACKET_START,
    PasteError,
    PasteReview,
    confirm_chunks,
)
from relaydeck.client.features.terminal.paste_policy import (
    character_count as policy_character_count,
)
from relaydeck.client.features.terminal.paste_policy import (
    line_count as policy_line_count,
)
from relaydeck.client.features.terminal.paste_review import (
    CANCEL_KEY,
    CONFIRM_KEY,
    COUNTS_KEY,
    PREVIEW_KEY,
    REQUEST_BLOCKED,
    REQUEST_INSERTED,
    REQUEST_REJECTED,
    REQUEST_REVIEW,
    SHEET_KEY,
    TITLE_KEY,
    PasteReviewSession,
    PasteReviewSheet,
    bind_review_session,
    counts_line,
)
from relaydeck.client.features.terminal.projection import TerminalProjection
from relaydeck.contracts.wire import terminal as terminal_wire
from tests.support.host_harness import render_component, run

VT = Path(__file__).resolve().parent.parent / "fixtures" / "vt"


class _Writer:
    def __init__(self, generation: int | None = 3) -> None:
        self.generation = generation
        self.sent: list[bytes] = []

    def probe(self) -> int | None:
        return self.generation

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    def session(self) -> PasteReviewSession:
        return PasteReviewSession(self.probe, self.send)


class _FlakyWriter(_Writer):
    """Fails the second chunk once, then succeeds (mid-send failure)."""

    def __init__(self) -> None:
        super().__init__()
        self.armed = True
        self.calls = 0

    async def send(self, data: bytes) -> None:
        self.calls += 1
        if self.armed and self.calls == 2:
            raise TransportError("host_offline", "send failed mid-paste")
        self.sent.append(data)


class _ScriptedSocket(TerminalSocket):
    def __init__(self) -> None:
        self.sent_bytes: list[bytes] = []
        self.sent_text: list[str] = []
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def send_text(self, data: str) -> None:
        self.sent_text.append(data)

    async def receive(self) -> tuple[str, Any]:
        raise TransportError("host_offline", "eof")

    async def close(self) -> None:
        self.closed = True


def test_single_line_inserts_immediately_unframed() -> None:
    async def scenario() -> None:
        writer = _Writer()
        session = writer.session()
        assert await session.request_paste("echo hi", bracketed=True) == REQUEST_INSERTED
        assert writer.sent == [b"echo hi"]
        assert session.pending is None
        assert session.error is None

    run(scenario())


def test_empty_paste_inserts_nothing() -> None:
    async def scenario() -> None:
        writer = _Writer()
        session = writer.session()
        assert await session.request_paste("", bracketed=True) == REQUEST_INSERTED
        assert writer.sent == []
        assert session.pending is None

    run(scenario())


def test_multiline_opens_review_with_exact_counts() -> None:
    async def scenario() -> None:
        writer = _Writer()
        session = writer.session()
        text = "a\nb\tc\U0001f600"
        assert await session.request_paste(text, bracketed=True) == REQUEST_REVIEW
        assert writer.sent == []
        pending = session.pending
        assert pending is not None
        assert pending.generation == 3
        assert pending.bracketed is True
        assert pending.text == text
        assert pending.review.preview == text
        assert pending.review.line_count == policy_line_count(text) == 2
        assert pending.review.character_count == policy_character_count(text) == 6

    run(scenario())


def test_confirm_sends_exact_framed_bytes() -> None:
    async def scenario() -> None:
        writer = _Writer()
        session = writer.session()
        text = "printf 'a'\nprintf 'b'"
        assert await session.request_paste(text, bracketed=True) == REQUEST_REVIEW
        assert await session.confirm_paste() is True
        assert writer.sent == [BRACKET_START + text.encode() + BRACKET_END]
        assert session.pending is None
        assert await session.confirm_paste() is False
        assert session.error == "No paste awaiting review."
        assert writer.sent == [BRACKET_START + text.encode() + BRACKET_END]

    run(scenario())


def test_confirm_unbracketed_exact_bytes() -> None:
    async def scenario() -> None:
        writer = _Writer()
        session = writer.session()
        text = "a\r\nb\rc"
        assert await session.request_paste(text, bracketed=False) == REQUEST_REVIEW
        assert await session.confirm_paste() is True
        assert writer.sent == [text.encode()]
        assert writer.sent[0] == b"a\r\nb\rc"

    run(scenario())


def test_confirm_mid_send_failure_clears_pending() -> None:
    async def scenario() -> None:
        text = "a\n" + "y" * terminal_wire.MAX_INPUT_CHUNK
        expected = confirm_chunks(text, True)
        assert len(expected) == 2
        writer = _FlakyWriter()
        session = writer.session()
        assert await session.request_paste(text, bracketed=True) == REQUEST_REVIEW
        with pytest.raises(TransportError):
            await session.confirm_paste()
        assert session.pending is None
        assert writer.sent == [expected[0]]
        # No implicit retry: a bare re-confirm sends nothing more.
        assert await session.confirm_paste() is False
        assert writer.sent == [expected[0]]
        # After re-open, the retry sends the exact bytes once.
        writer.armed = False
        assert await session.request_paste(text, bracketed=True) == REQUEST_REVIEW
        assert await session.confirm_paste() is True
        assert writer.sent[1:] == list(expected)
        assert b"".join(writer.sent[1:]) == BRACKET_START + text.encode() + BRACKET_END

    run(scenario())


def test_cancel_sends_nothing() -> None:
    async def scenario() -> None:
        writer = _Writer()
        session = writer.session()
        assert await session.request_paste("a\nb", bracketed=True) == REQUEST_REVIEW
        session.cancel_paste()
        assert session.pending is None
        assert writer.sent == []
        assert await session.confirm_paste() is False
        assert writer.sent == []

    run(scenario())


def test_stale_generation_fails_closed() -> None:
    async def scenario() -> None:
        writer = _Writer()
        session = writer.session()
        assert await session.request_paste("a\nb", bracketed=True) == REQUEST_REVIEW
        writer.generation = 4
        assert await session.confirm_paste() is False
        assert writer.sent == []
        assert session.pending is None
        assert session.error is not None and "Nothing was sent" in session.error

    run(scenario())


def test_dead_writer_fails_closed() -> None:
    async def scenario() -> None:
        writer = _Writer()
        session = writer.session()
        assert await session.request_paste("a\nb", bracketed=False) == REQUEST_REVIEW
        writer.generation = None
        assert await session.confirm_paste() is False
        assert writer.sent == []
        assert session.pending is None

    run(scenario())


@pytest.mark.parametrize("text", ["echo hi", "a\nb"])
def test_no_writer_blocks_request(text: str) -> None:
    async def scenario() -> None:
        writer = _Writer(generation=None)
        session = writer.session()
        assert await session.request_paste(text, bracketed=True) == REQUEST_BLOCKED
        assert session.pending is None
        assert writer.sent == []
        assert session.error is not None and "no live control writer" in session.error

    run(scenario())


def test_unsafe_rejected_with_reason() -> None:
    async def scenario() -> None:
        writer = _Writer()
        session = writer.session()
        outcome = await session.request_paste("a\x1b\nb", bracketed=True)
        assert outcome == REQUEST_REJECTED
        assert session.pending is None
        assert writer.sent == []
        assert session.error is not None and "U+001B" in session.error
        assert "offset 1" in session.error

    run(scenario())


def test_rejection_clears_open_review() -> None:
    async def scenario() -> None:
        writer = _Writer()
        session = writer.session()
        assert await session.request_paste("a\nb", bracketed=True) == REQUEST_REVIEW
        assert session.pending is not None
        assert await session.request_paste("bad\x07", bracketed=True) == REQUEST_REJECTED
        assert session.pending is None
        assert writer.sent == []

    run(scenario())


def test_review_replaces_pending_and_rebinds() -> None:
    async def scenario() -> None:
        writer = _Writer()
        session = writer.session()
        assert await session.request_paste("aaa\nbbb", bracketed=True) == REQUEST_REVIEW
        writer.generation = 4
        assert await session.request_paste("c\nd", bracketed=False) == REQUEST_REVIEW
        pending = session.pending
        assert pending is not None and pending.generation == 4
        assert pending.review.preview == "c\nd"
        assert await session.confirm_paste() is True
        assert writer.sent == [b"c\nd"]

    run(scenario())


def test_single_line_insert_leaves_open_review() -> None:
    async def scenario() -> None:
        writer = _Writer()
        session = writer.session()
        assert await session.request_paste("a\nb", bracketed=True) == REQUEST_REVIEW
        assert await session.request_paste("ls", bracketed=True) == REQUEST_INSERTED
        assert writer.sent == [b"ls"]
        assert session.pending is not None
        assert await session.confirm_paste() is True
        assert writer.sent == [b"ls", BRACKET_START + b"a\nb" + BRACKET_END]

    run(scenario())


def test_no_enter_in_session_flows() -> None:
    async def scenario() -> None:
        writer = _Writer()
        session = writer.session()
        assert await session.request_paste("echo hi", bracketed=True) == REQUEST_INSERTED
        assert await session.request_paste("x\ny", bracketed=True) == REQUEST_REVIEW
        assert await session.confirm_paste() is True
        assert await session.request_paste("p\nq", bracketed=False) == REQUEST_REVIEW
        assert await session.confirm_paste() is True
        (single, framed, plain) = writer.sent
        assert single == b"echo hi"
        assert framed == BRACKET_START + b"x\ny" + BRACKET_END
        assert plain == b"p\nq"
        assert framed.endswith(BRACKET_END)
        assert not framed.endswith(BRACKET_END + b"\r")

    run(scenario())


def test_sheet_renders_counts_preview_and_buttons() -> None:
    async def scenario() -> None:
        review = PasteReview(line_count=2, character_count=6, preview="a\nb\tc")
        calls: list[str] = []
        app = await render_component(
            PasteReviewSheet,
            review=review,
            on_confirm=lambda: calls.append("confirm"),
            on_cancel=lambda: calls.append("cancel"),
        )
        assert app.find(SHEET_KEY) is not None
        assert app.find(TITLE_KEY).value == "Review paste"
        assert app.find(COUNTS_KEY).value == counts_line(review) == "2 lines \u00b7 6 characters"
        assert app.find(PREVIEW_KEY).value == "a\nb\tc"
        assert app.find(CONFIRM_KEY).content == "Paste"
        assert app.find(CANCEL_KEY).content == "Cancel"
        await app.set_props_and_fire(CONFIRM_KEY, {}, "click")
        await app.set_props_and_fire(CANCEL_KEY, {}, "click")
        assert calls == ["confirm", "cancel"]

    run(scenario())


def test_engine_bracketed_mode_drives_framing() -> None:
    async def scenario() -> None:
        engine = TerminalProjection()
        engine.feed((VT / "07a_bracketed_on.bin").read_bytes())
        assert engine.bracketed_paste is True
        writer = _Writer()
        session = writer.session()
        text = "a\nb"
        assert await session.request_paste(text, engine.bracketed_paste) == REQUEST_REVIEW
        assert await session.confirm_paste() is True
        engine.feed((VT / "07b_bracketed_off.bin").read_bytes())
        engine.flush()
        assert engine.bracketed_paste is False
        assert await session.request_paste(text, engine.bracketed_paste) == REQUEST_REVIEW
        assert await session.confirm_paste() is True
        assert writer.sent == [
            BRACKET_START + b"a\nb" + BRACKET_END,
            b"a\nb",
        ]

    run(scenario())


def test_framing_captured_at_open_ignores_later_engine_flip() -> None:
    async def scenario() -> None:
        engine = TerminalProjection()
        writer = _Writer()
        session = writer.session()
        text = "a\nb"
        engine.feed((VT / "07a_bracketed_on.bin").read_bytes())
        engine.flush()
        assert engine.bracketed_paste is True
        assert await session.request_paste(text, engine.bracketed_paste) == REQUEST_REVIEW
        engine.feed((VT / "07b_bracketed_off.bin").read_bytes())
        engine.flush()
        assert engine.bracketed_paste is False
        assert await session.confirm_paste() is True
        assert writer.sent == [BRACKET_START + b"a\nb" + BRACKET_END]

    run(scenario())


def test_unframed_captured_at_open_ignores_later_engine_flip() -> None:
    async def scenario() -> None:
        engine = TerminalProjection()
        writer = _Writer()
        session = writer.session()
        text = "a\nb"
        engine.feed((VT / "07b_bracketed_off.bin").read_bytes())
        engine.flush()
        assert engine.bracketed_paste is False
        assert await session.request_paste(text, engine.bracketed_paste) == REQUEST_REVIEW
        engine.feed((VT / "07a_bracketed_on.bin").read_bytes())
        engine.flush()
        assert engine.bracketed_paste is True
        assert await session.confirm_paste() is True
        assert writer.sent == [b"a\nb"]

    run(scenario())


# -- controller integration: sole current writer -------------------------------


def _controller(sock: _ScriptedSocket, mode: str, generation: int = 1) -> TerminalController:
    return TerminalController(
        socket_factory=lambda ticket: sock,
        lease_id="lease-1",
        generation=generation,
        mode=mode,
        on_bytes=lambda data: None,
    )


def test_controller_confirm_sends_text_frames() -> None:
    async def scenario() -> None:
        sock = _ScriptedSocket()
        controller = _controller(sock, "control")
        await controller.start("rdt_test")
        assert controller.paste_generation() == 1
        session = bind_review_session(controller)
        text = "git status\ngit diff"
        assert await session.request_paste(text, bracketed=False) == REQUEST_REVIEW
        assert sock.sent_bytes == []
        assert await session.confirm_paste() is True
        assert len(sock.sent_bytes) == 1
        chunk = terminal_wire.decode_input(sock.sent_bytes[0])
        assert (chunk.lease_id, chunk.generation, chunk.kind) == ("lease-1", 1, "text")
        assert chunk.input_seq == 1
        assert chunk.data == text.encode()

    run(scenario())


def test_controller_bracketed_confirm_exact() -> None:
    async def scenario() -> None:
        sock = _ScriptedSocket()
        controller = _controller(sock, "control", generation=7)
        await controller.start("rdt_test")
        session = bind_review_session(controller)
        assert await session.request_paste("a\nb", bracketed=True) == REQUEST_REVIEW
        assert await session.confirm_paste() is True
        (frame,) = sock.sent_bytes
        chunk = terminal_wire.decode_input(frame)
        assert chunk.generation == 7
        assert chunk.data == BRACKET_START + b"a\nb" + BRACKET_END

    run(scenario())


def test_controller_multi_chunk_confirm_input_seq_continuity() -> None:
    async def scenario() -> None:
        sock = _ScriptedSocket()
        controller = _controller(sock, "control")
        await controller.start("rdt_test")
        session = bind_review_session(controller)
        text = "a\n" + "x" * (2 * terminal_wire.MAX_INPUT_CHUNK)
        assert await session.request_paste(text, bracketed=True) == REQUEST_REVIEW
        assert await session.confirm_paste() is True
        chunks = [terminal_wire.decode_input(frame) for frame in sock.sent_bytes]
        assert [chunk.input_seq for chunk in chunks] == [1, 2, 3]
        assert all(
            (chunk.lease_id, chunk.generation, chunk.kind) == ("lease-1", 1, "text")
            for chunk in chunks
        )
        assert all(len(chunk.data) <= terminal_wire.MAX_INPUT_CHUNK for chunk in chunks)
        assert b"".join(chunk.data for chunk in chunks) == (
            BRACKET_START + text.encode() + BRACKET_END
        )

    run(scenario())


def test_controller_single_line_uses_input_seq() -> None:
    async def scenario() -> None:
        sock = _ScriptedSocket()
        controller = _controller(sock, "control")
        await controller.start("rdt_test")
        session = bind_review_session(controller)
        assert await session.request_paste("ls", bracketed=True) == REQUEST_INSERTED
        assert await session.request_paste("pwd", bracketed=True) == REQUEST_INSERTED
        seqs = [terminal_wire.decode_input(frame).input_seq for frame in sock.sent_bytes]
        assert seqs == [1, 2]

    run(scenario())


def test_controller_view_mode_blocks() -> None:
    async def scenario() -> None:
        sock = _ScriptedSocket()
        controller = _controller(sock, "view")
        await controller.start("rdt_test")
        assert controller.paste_generation() is None
        session = bind_review_session(controller)
        assert await session.request_paste("a\nb", bracketed=True) == REQUEST_BLOCKED
        assert sock.sent_bytes == []

    run(scenario())


def test_controller_closed_blocks_and_sender_raises() -> None:
    async def scenario() -> None:
        sock = _ScriptedSocket()
        controller = _controller(sock, "control")
        await controller.start("rdt_test")
        await controller.close()
        assert controller.paste_generation() is None
        session = bind_review_session(controller)
        assert await session.request_paste("a\nb", bracketed=True) == REQUEST_BLOCKED
        with pytest.raises(PasteError) as exc:
            await controller.send_paste_chunk(b"a\nb")
        assert exc.value.code == "no_writer"
        assert sock.sent_bytes == []

    run(scenario())


def test_controller_stale_generation_sends_no_frames() -> None:
    async def scenario() -> None:
        sock = _ScriptedSocket()
        controller = _controller(sock, "control")
        await controller.start("rdt_test")
        session = bind_review_session(controller)
        assert await session.request_paste("rm -rf /\necho kept", bracketed=True) == REQUEST_REVIEW
        controller.generation = 2
        assert await session.confirm_paste() is False
        assert sock.sent_bytes == []

    run(scenario())


def test_send_paste_chunk_no_writer_raises_explicit() -> None:
    async def scenario() -> None:
        sock = _ScriptedSocket()
        controller = _controller(sock, "view")
        await controller.start("rdt_test")
        with pytest.raises(PasteError) as exc:
            await controller.send_paste_chunk(b"x")
        assert exc.value.code == "no_writer"
        assert sock.sent_bytes == []

    run(scenario())
