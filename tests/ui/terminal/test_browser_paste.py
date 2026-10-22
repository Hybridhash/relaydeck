"""Cmd/Ctrl+V in a desktop browser: the paste event's text reaches the terminal as a `text` event
on the paste region and goes through the reviewed paste, never a clipboard read."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from relaydeck.client.features.terminal.diagnostics import diagnostics_logger
from relaydeck.client.features.terminal.paste_policy import BRACKET_END, BRACKET_START
from relaydeck.client.features.terminal.paste_review import CONFIRM_KEY
from relaydeck.client.features.terminal.pictures import MAX_PASTE_TEXT
from relaydeck.contracts.wire.terminal import MAX_INPUT_CHUNK
from tests.support.fakes import FakeClipboard
from tests.support.host_harness import run
from tests.support.terminal.direct_typing import _direct, open_compose
from tests.support.terminal.latch_text import _inputs

REGION = {False: "agent-picture-paste", True: "shell-picture-paste"}
BLOCKED = "Paste blocked by the browser: use the paste panel"


class _NeverRead(FakeClipboard):
    async def get_text(self) -> str:
        raise AssertionError("a browser paste never reads the clipboard")


async def _paste(app: Any, shell: bool, text: str, binding: str | None = None) -> None:
    # A real browser echoes the binding the region held at the gesture.
    held = app.find(REGION[shell]).binding if binding is None else binding
    await app.set_props_and_fire(REGION[shell], {}, "text", {"text": text, "binding": held})


@pytest.mark.parametrize("shell", [False, True])
def test_a_single_line_paste_inserts_once_without_a_clipboard_read(shell: bool) -> None:
    async def scenario() -> None:
        app, session, socket, _focuses = await _direct(shell)
        try:
            session._clipboard = _NeverRead()
            region = app.find(REGION[shell])
            assert region.pictures is False  # the harness gateway takes no pictures
            await _paste(app, shell, "echo hi")
            assert _inputs(socket) == [b"echo hi"]  # no Enter, no framing, one frame
            assert session.pending_review is None and session.paste_error is None
        finally:
            await session.dispose()

    run(scenario())


def test_a_multiline_paste_opens_review_and_sends_framed_bytes_only_on_confirm() -> None:
    async def scenario() -> None:
        app, session, socket, _focuses = await _direct(False)
        try:
            session._clipboard = _NeverRead()
            session.set_bracketed_paste(True)
            text = "echo one\necho two"
            await _paste(app, False, text)
            assert _inputs(socket) == []
            review = session.pending_review
            assert review is not None and review.preview == text and review.line_count == 2
            await app.set_props_and_fire(CONFIRM_KEY, {}, "click")
            assert b"".join(_inputs(socket)) == BRACKET_START + text.encode() + BRACKET_END
        finally:
            await session.dispose()

    run(scenario())


def test_control_characters_are_refused_with_the_policy_reason() -> None:
    async def scenario() -> None:
        app, session, socket, _focuses = await _direct(False)
        try:
            session._clipboard = _NeverRead()
            await _paste(app, False, "ls\x1b[A")
            assert _inputs(socket) == [] and session.pending_review is None
            assert (session.paste_error or "").startswith("unsafe control U+001B")
        finally:
            await session.dispose()

    run(scenario())


def test_a_paste_larger_than_one_frame_is_chunked_like_every_paste() -> None:
    async def scenario() -> None:
        app, session, socket, _focuses = await _direct(False)
        try:
            session._clipboard = _NeverRead()
            text = "x" * (MAX_INPUT_CHUNK + 100)
            await _paste(app, False, text)
            frames = _inputs(socket)
            assert len(frames) == 2 and all(len(frame) <= MAX_INPUT_CHUNK for frame in frames)
            assert b"".join(frames) == text.encode()
        finally:
            await session.dispose()

    run(scenario())


def test_an_empty_text_event_sends_nothing() -> None:
    async def scenario() -> None:
        app, session, socket, _focuses = await _direct(False)
        try:
            session._clipboard = _NeverRead()
            await _paste(app, False, "")
            assert _inputs(socket) == [] and session.pending_review is None
        finally:
            await session.dispose()

    run(scenario())


def test_a_paste_without_text_shows_the_blocked_message() -> None:
    async def scenario() -> None:
        app, session, _socket, _focuses = await _direct(False)
        try:
            await app.set_props_and_fire(REGION[False], {}, "reject", {"reason": "no_text"})
            assert app.find("picture-upload-message").value == BLOCKED
        finally:
            await session.dispose()

    run(scenario())


def test_the_pasted_text_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    secret = "s3cr3t-paste-9f8e7d"
    diagnostics = diagnostics_logger()
    diagnostics.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG)

    async def scenario() -> None:
        app, session, socket, _focuses = await _direct(False)
        try:
            session._clipboard = _NeverRead()
            await _paste(app, False, secret)
            await _paste(app, False, secret + "\nsecond line")
            assert _inputs(socket) == [secret.encode()]
        finally:
            await session.dispose()

    try:
        run(scenario())
    finally:
        diagnostics.removeHandler(caplog.handler)
    assert secret not in caplog.text


def test_compose_mode_never_types_a_paste_into_the_display_only_terminal() -> None:
    async def scenario() -> None:
        app, session, socket, _focuses = await _direct(False)
        try:
            await open_compose(app)
            before = list(_inputs(socket))
            await _paste(app, False, "rm -rf build")
            assert _inputs(socket) == before
            assert session.pending_review is None
        finally:
            await session.dispose()

    run(scenario())


def test_text_over_the_cap_is_refused_and_nothing_is_sent() -> None:
    async def scenario() -> None:
        app, session, socket, _focuses = await _direct(False)
        try:
            await _paste(app, False, "x" * (MAX_PASTE_TEXT + 1))
            assert _inputs(socket) == []
            assert "larger than 1 MB" in app.find("picture-upload-message").value
        finally:
            await session.dispose()

    run(scenario())


def test_a_late_paste_from_an_older_binding_is_refused_and_sends_nothing() -> None:
    async def scenario() -> None:
        app, session, socket, _focuses = await _direct(False)
        try:
            stale = app.find(REGION[False]).binding
            await _paste(app, False, "echo late", binding=stale + "-old")
            assert _inputs(socket) == []
            assert session.pending_review is None
            assert "nothing was pasted" in app.find("picture-upload-message").value
        finally:
            await session.dispose()

    run(scenario())
