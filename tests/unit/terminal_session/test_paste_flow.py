"""Q5d: PasteFlow alone, over the real generation-bound PasteReviewSession."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from relaydeck.client.adapters.clipboard import ClipboardReadStatus
from relaydeck.client.adapters.ports import ClipboardPort
from relaydeck.client.features.terminal.paste_policy import BRACKET_END, BRACKET_START
from relaydeck.client.features.terminal.paste_review import (
    REQUEST_BLOCKED,
    REQUEST_INSERTED,
    REQUEST_REJECTED,
    REQUEST_REVIEW,
    STALE_MESSAGE,
    PasteReviewSession,
)
from relaydeck.client.features.terminal.session.paste import PasteFlow, PasteHooks

MULTILINE = "echo one\necho two"


class Clipboard(ClipboardPort):
    def __init__(self, text: str | None = "pasted", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.reads = 0
        self.on_read: Any = None

    async def get_text(self) -> str | None:
        self.reads += 1
        if self.on_read is not None:
            self.on_read()
        if self.error is not None:
            raise self.error
        return self.text

    async def set_text(self, value: str) -> None:
        self.text = value


class Rig:
    def __init__(self, *, generation: int | None = 1, clipboard: Clipboard | None = None) -> None:
        self.generation = generation
        self.sent: list[bytes] = []
        self.notified = 0
        self.bracketed = False
        self.token: Any = (1, 1)
        self.interaction = 0
        self.bound = generation is not None
        self.clipboard = clipboard
        self.review = PasteReviewSession(lambda: self.generation, self._send)
        self.flow = PasteFlow(
            PasteHooks(
                bound=lambda: self.bound,
                review=lambda: self.review,
                bracketed=lambda: self.bracketed,
                input_token=lambda: self.token,
                interaction_generation=lambda: self.interaction,
                clipboard=lambda: self.clipboard,
                notify=self._notify,
            )
        )

    async def _send(self, data: bytes) -> None:
        self.sent.append(data)

    def _notify(self) -> None:
        self.notified += 1


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_nothing_happens_without_a_bound_writer() -> None:
    async def go() -> None:
        rig = Rig(generation=None)
        assert await rig.flow.request("ls") == REQUEST_BLOCKED
        assert await rig.flow.confirm() is False
        rig.flow.cancel()
        assert rig.sent == [] and rig.notified == 0

    run(go())


def test_single_line_inserts_immediately_and_notifies() -> None:
    async def go() -> None:
        rig = Rig()
        assert await rig.flow.request("ls -la") == REQUEST_INSERTED
        assert rig.sent and rig.notified == 1

    run(go())


def test_multiline_only_opens_a_review_and_sends_nothing() -> None:
    async def go() -> None:
        rig = Rig()
        assert await rig.flow.request(MULTILINE) == REQUEST_REVIEW
        assert rig.review.pending is not None and rig.sent == []

    run(go())


def test_explicit_confirm_sends_exactly_the_reviewed_text_once() -> None:
    async def go() -> None:
        rig = Rig()
        rig.bracketed = True
        await rig.flow.request(MULTILINE)
        assert await rig.flow.confirm() is True
        payload = b"".join(rig.sent)
        assert payload.startswith(BRACKET_START) and payload.endswith(BRACKET_END)
        assert MULTILINE.encode() in payload
        sent = list(rig.sent)
        # A second confirm has nothing pending: nothing is replayed.
        assert await rig.flow.confirm() is False
        assert rig.sent == sent

    run(go())


def test_stale_confirm_is_rejected_and_sends_nothing() -> None:
    async def go() -> None:
        rig = Rig(generation=1)
        await rig.flow.request(MULTILINE)
        rig.generation = 2  # the writer was replaced after the review opened
        assert await rig.flow.confirm() is False
        assert rig.sent == []
        assert rig.review.error == STALE_MESSAGE and rig.review.pending is None
        # The retired review cannot come back on the new writer.
        assert await rig.flow.confirm() is False and rig.sent == []

    run(go())


def test_confirm_after_the_writer_died_is_rejected() -> None:
    async def go() -> None:
        rig = Rig(generation=1)
        await rig.flow.request(MULTILINE)
        rig.generation = None
        assert await rig.flow.confirm() is False and rig.sent == []

    run(go())


def test_confirm_is_a_no_op_once_the_session_is_unbound() -> None:
    async def go() -> None:
        rig = Rig()
        await rig.flow.request(MULTILINE)
        rig.bound = False
        assert await rig.flow.confirm() is False and rig.sent == []

    run(go())


def test_cancel_drops_the_review_and_provably_sends_nothing() -> None:
    async def go() -> None:
        rig = Rig()
        await rig.flow.request(MULTILINE)
        rig.flow.cancel()
        assert rig.review.pending is None and rig.sent == []
        assert await rig.flow.confirm() is False and rig.sent == []

    run(go())


def test_unsafe_controls_are_rejected_not_reviewed() -> None:
    async def go() -> None:
        rig = Rig()
        assert await rig.flow.request("bad\x1b[31m") == REQUEST_REJECTED
        assert rig.review.pending is None and rig.sent == []

    run(go())


def test_clipboard_paste_reads_once_and_classifies() -> None:
    async def go() -> None:
        clip = Clipboard("ls")
        rig = Rig(clipboard=clip)
        assert await rig.flow.from_clipboard() == REQUEST_INSERTED
        assert clip.reads == 1

        multi = Rig(clipboard=Clipboard(MULTILINE))
        assert await multi.flow.from_clipboard() == REQUEST_REVIEW
        assert multi.sent == []  # review first, never sent by the read

    run(go())


def test_clipboard_unavailable_returns_the_status_and_sends_nothing() -> None:
    async def go() -> None:
        rig = Rig(clipboard=None)
        assert await rig.flow.from_clipboard() == ClipboardReadStatus.UNAVAILABLE
        assert rig.sent == []

    run(go())


@pytest.mark.parametrize("change", ["token", "interaction"])
def test_clipboard_result_is_dropped_if_the_input_changed_while_reading(change: str) -> None:
    async def go() -> None:
        clip = Clipboard(MULTILINE)
        rig = Rig(clipboard=clip)

        def mutate() -> None:
            if change == "token":
                rig.token = (1, 2)
            else:
                rig.interaction += 1

        clip.on_read = mutate
        assert await rig.flow.from_clipboard() == REQUEST_BLOCKED
        assert rig.review.pending is None and rig.sent == []

    run(go())


def test_app_copied_text_always_needs_review() -> None:
    async def go() -> None:
        class AppCopy(Clipboard):
            def copied_text(self) -> str:
                return "ls"

        clip = AppCopy(error=RuntimeError("blocked"))
        rig = Rig(clipboard=clip)
        assert await rig.flow.from_clipboard() == REQUEST_REVIEW
        assert rig.review.pending is not None and rig.review.pending.review.from_app
        assert rig.sent == []

    run(go())


def test_stale_review_notice_text() -> None:
    assert PasteFlow.stale_review_notice() == STALE_MESSAGE
