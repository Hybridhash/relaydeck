"""Q5a: shared session contracts, green before any Q5 extraction.

Only construction, explicit takeover and resume disclosure differ by session.
Tests exercise real sessions/controllers/renewal and decode their wire frames;
gateway, socket, clipboard and renderer are synthetic ports. Agent Composer is
not conflated with Direct input: its untouched draft is a separate invariant.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

import pytest
from relaydeck_terminal.protocol import ORIGIN_KEY, ORIGIN_MOUSE, ORIGIN_TEXT

from relaydeck.client.features.shell.session import ShellSession
from relaydeck.client.features.sync.transport import TransportError
from relaydeck.client.features.terminal.agent_session import AgentSession
from relaydeck.client.features.terminal.input_modes import WORKFLOW_AGENT_DIRECT
from relaydeck.client.features.terminal.paste_policy import BRACKET_END, BRACKET_START
from relaydeck.client.features.terminal.paste_review import STALE_MESSAGE
from relaydeck.contracts.wire.terminal import InputChunk, decode_input, encode_output
from tests.support.scripted_sessions import (
    ScriptedSessions,
    ScriptedTerminalSocket,
    make_scripted_sessions,
)

Session = AgentSession | ShellSession
SessionType = type[AgentSession] | type[ShellSession]
Renew = Callable[[str], Awaitable[dict[str, Any]]]
DRAFT = "unsent composer text\nkept verbatim"


@pytest.fixture(params=[AgentSession, ShellSession], ids=["agent", "shell"])
def session_type(request: pytest.FixtureRequest) -> SessionType:
    return cast(SessionType, request.param)


async def until(predicate: Callable[[], bool]) -> None:
    """Wait for an observed event, not a wall-clock guess."""
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


@dataclass
class Harness:
    session: Session
    ports: ScriptedSessions

    @property
    def socket(self) -> ScriptedTerminalSocket:
        return self.ports.streams.opened[-1][2]

    @property
    def frames(self) -> list[InputChunk]:
        return [
            decode_input(data)
            for _, _, socket in self.ports.streams.opened
            for kind, data in socket.sent
            if kind == "bytes"
        ]

    async def take_over(self) -> None:
        if isinstance(self.session, AgentSession):
            self.session.request_takeover()
            result = await self.session.confirm_takeover()
        else:
            result = await self.session.take_over()
        assert result == "live"

    def assert_draft_untouched(self) -> None:
        if isinstance(self.session, AgentSession):
            assert self.session.composer_props()["draft"] == DRAFT
        assert self.ports.submitted == [] and self.ports.prompted == []

    async def output(self, data: bytes) -> None:
        lease = self.session.lease
        assert lease is not None
        offset = len(self.session.output_log)
        self.socket.queue(("bytes", encode_output(lease.epoch, offset, data)))
        await until(lambda: len(self.session.output_log) == offset + len(data))


@asynccontextmanager
async def connected(
    session_type: SessionType, *, renew: Renew | None = None
) -> AsyncIterator[Harness]:
    ports = make_scripted_sessions()
    if renew is not None:
        ports.factory.renew_lease = renew
    if session_type is AgentSession:
        session: Session = ports.factory.make_agent(
            host_id="mac", host_epoch="h1", agent_id="pane", target="pane"
        )
    else:
        session = ports.factory.make_shell(host_id="mac")
    assert type(session) is session_type
    try:
        if isinstance(session, AgentSession):
            session.open()
            assert await session.confirm_takeover() == "live"
            session.set_workflow(WORKFLOW_AGENT_DIRECT)
            session.on_draft_change(DRAFT)
        else:
            assert await session.open("pane") == "live"
        assert session.keys_enabled
        yield Harness(session, ports)
    finally:
        await session.dispose(clear_sensitive=True)


class Renderer:
    """Small renderer port; no Flutter/layout claims."""

    def __init__(self, snapshot: str = "alpha\nbeta\nγ") -> None:
        self.snapshot = snapshot
        self.output: list[bytes] = []
        self.input_enabled = True
        self.clears = 0
        self.resets: list[str] = []

    def set_input_enabled(self, enabled: bool) -> None:
        self.input_enabled = enabled

    def reset(self) -> None:
        self.resets.append("reset")

    async def get_snapshot_text(self) -> str:
        return self.snapshot

    def clear_selection(self) -> None:
        self.clears += 1


def render_write(term: Renderer, data: bytes) -> bool:
    term.output.append(data)
    return True


def test_renderer_tokens_route_output_and_survive_stale_unbind(session_type: SessionType) -> None:
    async def scenario() -> None:
        async with connected(session_type) as h:
            s = h.session
            one: list[bytes] = []
            two: list[bytes] = []
            first = s.bind_renderer(one.append)
            second = s.bind_renderer(two.append)
            task = asyncio.create_task(s.run())
            try:
                await h.output(b"first")
                assert one == two == [b"first"]
                s.unbind_renderer(first)
                s.unbind_renderer(first)  # Stale/idempotent cleanup cannot remove the winner.
                await h.output(b"second")
                assert one == [b"first"] and two == [b"first", b"second"]
                assert not s.is_sink_live(first) and s.is_sink_live(second)
                assert bytes(s.output_log) == b"firstsecond" and h.frames == []
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_view_unmount_remount_fences_callbacks_and_drops_pending_input(
    session_type: SessionType,
) -> None:
    async def scenario() -> None:
        async with connected(session_type) as h:
            s, one, two = h.session, Renderer(), Renderer()
            controller, lease = s.controller, s.lease
            first = s.bind_view(one, write=render_write)
            old_token = s.input_token
            s.sessions.keys.commit_text("queued, never replay")
            s.toggle_modifier("ctrl")
            assert await s.request_paste("unconfirmed\npaste") == "review"
            s.unbind_view()
            assert not s.is_sink_live(first) and s.current_term is None
            assert not s.sessions.bound and not s.renewal_running
            assert s.pending_review is None and not s.armed_modifiers
            assert await s.handle_view_input(ORIGIN_TEXT, b"late", token=old_token) == "dropped"
            second = s.bind_view(two, write=render_write)
            s.unbind_renderer(first)
            assert s.current_term is two and s.is_sink_live(second)
            assert s.controller is controller and s.lease is lease
            assert s.input_token != old_token and s.renewal_running
            assert await s.flush_keys() == 0 and not await s.confirm_paste()
            assert await s.handle_view_input(ORIGIN_TEXT, b"fresh", token=s.input_token) == "pty"
            assert [frame.data for frame in h.frames] == [b"fresh"]
            h.assert_draft_untouched()

    asyncio.run(scenario())


def test_view_replacement_preserves_live_review_until_real_unmount(
    session_type: SessionType,
) -> None:
    async def scenario() -> None:
        async with connected(session_type) as h:
            s = h.session
            first = s.bind_view(Renderer(), write=render_write)
            token = s.input_token
            assert await s.request_paste("review\nme") == "review"
            review = s.pending_review
            second = s.bind_view(Renderer(), write=render_write)
            assert not s.is_sink_live(first) and s.is_sink_live(second)
            assert s.input_token == token and s.pending_review is review
            assert await s.confirm_paste()
            assert [frame.data for frame in h.frames] == [b"review\nme"]
            assert not await s.confirm_paste()  # Confirm is one-shot, even after replacement.

    asyncio.run(scenario())


def test_renewal_start_stop_is_awaited_and_does_not_write_input(session_type: SessionType) -> None:
    async def scenario() -> None:
        entered, cancelled = asyncio.Event(), asyncio.Event()
        renewed: list[str] = []

        async def renew(lease_id: str) -> dict[str, Any]:
            renewed.append(lease_id)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            return {}

        async with connected(session_type, renew=renew) as h:
            s = h.session
            assert s.lease is not None
            s.start_lease_renewal(interval_s=0.001)
            await until(entered.is_set)
            assert s.renewal_running and renewed == [s.lease.lease_id]
            await s.stop_lease_renewal()
            assert cancelled.is_set() and not s.renewal_running
            await s.stop_lease_renewal()
            assert s.keys_enabled and h.frames == []
            assert renewed == [s.lease.lease_id]

    asyncio.run(scenario())


@pytest.mark.parametrize("code", ["lease_expired", "unauthenticated", "forbidden"])
def test_takeover_retires_renewal_before_replacement_and_late_failure_is_inert(
    session_type: SessionType, code: str
) -> None:
    async def scenario() -> None:
        entered, cancelling, retired = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def renew(_: str) -> dict[str, Any]:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelling.set()
                await retired.wait()
                raise TransportError(code, "retired driver", False) from None
            raise AssertionError("blocked renewal unexpectedly resumed")

        async with connected(session_type, renew=renew) as h:
            s = h.session
            s.start_lease_renewal(interval_s=0.001)
            await until(entered.is_set)
            old_controller, old_lease, old_socket = s.controller, s.lease, h.socket
            driver = s._renewal
            assert driver is not None and old_lease is not None
            late_failure = driver._on_terminal_failure
            assert late_failure is not None
            before = (len(h.ports.ops.attaches), len(h.ports.ops.leases))
            task = asyncio.create_task(h.take_over())
            try:
                await until(cancelling.is_set)
                assert s.controller is old_controller and s.lease is old_lease
                assert not old_socket.closed
                assert (len(h.ports.ops.attaches), len(h.ports.ops.leases)) == before
                retired.set()
                await task
                assert not driver.running and s.renewal_running
                assert s.controller is not old_controller and s.lease is not old_lease
                assert s.lease is not None and s.lease.generation > old_lease.generation
                late_failure(code)
                assert s.keys_enabled and s.controller is not None
                assert not s.controller.writer_revoked and not s.notice
                assert h.frames == []  # Replacement never replays text/resize.
                h.assert_draft_untouched()
            finally:
                retired.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("code", ["lease_expired", "unauthenticated", "forbidden"])
def test_current_renewal_failure_fences_input_and_preserves_unsent_draft(
    session_type: SessionType, code: str
) -> None:
    async def scenario() -> None:
        async def denied(_: str) -> dict[str, Any]:
            raise TransportError(code, "synthetic refusal", False)

        async with connected(session_type, renew=denied) as h:
            s = h.session
            s.start_lease_renewal(interval_s=0.001)
            await until(lambda: s.controller is not None and s.controller.writer_revoked)
            assert not s.keys_enabled and s._renewal is not None and s._renewal.invalidated
            assert not await s.press_key("enter")
            assert await s.request_paste("blocked") == "blocked"
            assert (
                await s.handle_view_input(ORIGIN_TEXT, b"blocked", token=s.input_token) == "dropped"
            )
            assert h.frames == [] and s.notice
            h.assert_draft_untouched()

    asyncio.run(scenario())


def test_pause_resume_keeps_process_but_never_replays_or_silently_takes_agent(
    session_type: SessionType,
) -> None:
    async def scenario() -> None:
        async with connected(session_type) as h:
            s = h.session
            assert s.attachment is not None and s.lease is not None
            attachment, lease, old_token = s.attachment, s.lease, s.input_token
            old_keys = s.sessions.keys
            old_keys.commit_text("never replay after pause")
            assert await s.request_paste("unconfirmed\nreview") == "review"
            assert await s.pause() == "disconnected"
            assert s.paused and not s.renewal_running and not s.keys_enabled
            assert h.socket.closed and s.lease is None
            assert h.ports.ops.releases == [lease.lease_id]
            assert not h.ports.ops.is_closed(attachment.attachment_id)
            s.start_lease_renewal(interval_s=0.001)
            assert not s.renewal_running
            assert await s.handle_view_input(ORIGIN_TEXT, b"hidden", token=old_token) == "dropped"
            before = (len(h.ports.ops.attaches), len(h.ports.ops.leases))
            resumed = await s.resume_after_pause()
            assert not s.paused
            if isinstance(s, AgentSession):
                assert resumed == "disconnected" and s.takeover_disclosure is not None
                assert (len(h.ports.ops.attaches), len(h.ports.ops.leases)) == before
                assert await s.confirm_takeover() == "live"
            else:
                assert resumed == "live"  # Explicit same-device resume is supported.
            assert s.input_token != old_token and s.keys_enabled
            assert await old_keys.flush() == 0
            assert await s.flush_keys() == 0 and not await s.confirm_paste()
            assert await s.handle_view_input(ORIGIN_TEXT, b"stale", token=old_token) == "dropped"
            assert h.frames == [] and not h.ports.ops.closes
            h.assert_draft_untouched()

    asyncio.run(scenario())


@pytest.mark.parametrize("clear_sensitive", [False, True], ids=["leave", "revocation"])
def test_dispose_releases_and_fences_with_explicit_sensitive_wipe_policy(
    session_type: SessionType, clear_sensitive: bool
) -> None:
    async def scenario() -> None:
        async with connected(session_type) as h:
            s, term = h.session, Renderer()
            assert s.lease is not None and s.attachment is not None
            lease, attachment = s.lease, s.attachment
            binding = s.bind_view(term, write=render_write)
            token = s.input_token
            reader = asyncio.create_task(s.run())
            try:
                await h.output(b"cached output")
                await s.open_selection_snapshot()
                assert s.selection_sheet_text == term.snapshot
                await s.dispose(clear_sensitive=clear_sensitive)
                assert s.disposed and not s.renewal_running and not s.keys_enabled
                assert not s.is_sink_live(binding) and s.current_term is None
                assert h.socket.closed and h.ports.ops.releases == [lease.lease_id]
                assert not h.ports.ops.is_closed(attachment.attachment_id)
                assert await s.handle_view_input(ORIGIN_TEXT, b"late", token=token) == "dropped"
                s.start_lease_renewal(interval_s=0.001)
                assert not s.renewal_running and h.frames == []
                if clear_sensitive:
                    assert not s.output_log and s.selection_sheet_text == "" and not s.notice
                    if isinstance(s, AgentSession):
                        assert s.composer_props()["draft"] == ""
                else:
                    assert bytes(s.output_log) == b"cached output"
                    assert s.selection_sheet_text == term.snapshot
                    h.assert_draft_untouched()
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)

    asyncio.run(scenario())


def test_selection_freezes_snapshot_copies_current_range_and_closes_explicitly(
    session_type: SessionType,
) -> None:
    async def scenario() -> None:
        async with connected(session_type) as h:
            s, term = h.session, Renderer()
            s.bind_view(term, write=render_write)
            await s.open_selection_snapshot(anchor=0, focus=5)
            assert s.term_state.selection.open and s.selection_sheet_text == "alpha\nbeta\nγ"
            term.snapshot = "new live output"
            s.move_selection(10, 6)
            assert await s.copy_selection(s.selection_sheet_text, 0, 5) == "beta"
            assert h.ports.clipboard.writes == ["beta"]
            assert s.term_state.selection.open and s.selection_sheet_text == "alpha\nbeta\nγ"
            s.close_selection()
            assert not s.term_state.selection.open and term.clears == 1
            assert h.frames == []
            h.assert_draft_untouched()

    asyncio.run(scenario())


def test_selection_without_renderer_uses_raw_output_without_input(
    session_type: SessionType,
) -> None:
    async def scenario() -> None:
        async with connected(session_type) as h:
            s = h.session
            reader = asyncio.create_task(s.run())
            try:
                await h.output(b"fallback\ntext")
                await s.open_selection_snapshot()
                assert s.selection_sheet_text == "fallback\ntext" and s.term_state.selection.open
                assert await s.copy_selection(s.selection_sheet_text, 0, 0) == "fallback\ntext"
                assert h.ports.clipboard.writes == ["fallback\ntext"] and h.frames == []
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("bracketed", [False, True])
def test_paste_review_is_explicit_one_shot_and_captures_framing(
    session_type: SessionType, bracketed: bool
) -> None:
    async def scenario() -> None:
        async with connected(session_type) as h:
            s = h.session
            s.set_bracketed_paste(bracketed)
            text = "one\r\ntwo\tγ"
            assert await s.request_paste(text) == "review"
            review = s.pending_review
            assert review is not None and review.preview == text and review.line_count == 2
            assert h.frames == []
            s.set_bracketed_paste(not bracketed)
            assert await s.confirm_paste() and s.pending_review is None
            expected = text.encode()
            if bracketed:
                expected = BRACKET_START + expected + BRACKET_END
            assert b"".join(frame.data for frame in h.frames) == expected
            before = list(h.frames)
            assert not await s.confirm_paste() and h.frames == before
            h.assert_draft_untouched()

    asyncio.run(scenario())


def test_paste_cancel_rejection_clipboard_and_fallback_review(session_type: SessionType) -> None:
    async def scenario() -> None:
        async with connected(session_type) as h:
            s = h.session
            assert await s.request_paste("cancel\nthis") == "review"
            s.cancel_paste()
            assert s.pending_review is None and not await s.confirm_paste()
            assert await s.request_paste("bad\x1b[2J") == "rejected"
            assert s.pending_review is None and s.paste_error and h.frames == []
            h.ports.clipboard.text = "clipboard γ"
            assert await s.paste_from_clipboard() == "inserted"
            assert h.ports.clipboard.reads == 1
            assert [frame.data for frame in h.frames] == ["clipboard γ".encode()]
            assert await s.request_paste("local copy", from_app=True) == "review"
            assert s.pending_review is not None and s.pending_review.from_app
            assert len(h.frames) == 1
            s.cancel_paste()
            h.assert_draft_untouched()

    asyncio.run(scenario())


def test_key_and_native_text_share_ordered_generation_bound_writer(
    session_type: SessionType,
) -> None:
    async def scenario() -> None:
        async with connected(session_type) as h:
            s = h.session
            assert s.lease is not None
            token = s.input_token
            assert await s.press_key("enter")
            s.toggle_modifier("ctrl")
            assert await s.handle_view_input(ORIGIN_TEXT, b"c", token=token) == "pty"
            assert not s.armed_modifiers
            assert await s.handle_view_input(ORIGIN_TEXT, "hello γ".encode(), token=token) == "pty"
            assert await s.handle_view_input(ORIGIN_KEY, b"\x1b[A", token=token) == "pty"
            assert await s.handle_view_input(ORIGIN_MOUSE, b"mouse", token=token) == "dropped"
            assert [frame.data for frame in h.frames] == [
                b"\r",
                b"\x03",
                "hello γ".encode(),
                b"\x1b[A",
            ]
            assert [frame.input_seq for frame in h.frames] == [1, 2, 3, 4]
            assert all(
                frame.lease_id == s.lease.lease_id
                and frame.generation == s.lease.generation
                and frame.kind == "text"
                for frame in h.frames
            )
            h.assert_draft_untouched()

    asyncio.run(scenario())


def test_failed_key_encoding_preserves_modifier_and_interrupt_discards_it(
    session_type: SessionType,
) -> None:
    async def scenario() -> None:
        async with connected(session_type) as h:
            s = h.session
            s.toggle_modifier("ctrl")
            assert not await s.press_key("up") and s.armed_modifiers == frozenset({"ctrl"})
            assert h.frames == []
            assert await s.press_key("d") and not s.armed_modifiers
            s.toggle_modifier("alt")
            s.toggle_modifier("shift")
            assert s.interrupt() and await s.flush_keys() == 1
            assert not s.armed_modifiers
            assert [frame.data for frame in h.frames] == [b"\x04", b"\x03"]

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["background", "unmount", "takeover"])
def test_boundary_drops_old_callbacks_keys_and_paste_without_replay(
    session_type: SessionType, boundary: str
) -> None:
    async def scenario() -> None:
        async with connected(session_type) as h:
            s, term = h.session, Renderer()
            s.bind_view(term, write=render_write)
            token, old_keys = s.input_token, s.sessions.keys
            old_keys.commit_text("queued key")
            s.toggle_modifier("ctrl")
            assert await s.request_paste("old\nreview") == "review"
            before = (len(h.ports.ops.attaches), len(h.ports.ops.leases))
            if boundary == "background":
                controller = s.controller
                s.set_foreground(False)
                assert not term.input_enabled and s.input_token is None
                assert await s.handle_view_input(ORIGIN_TEXT, b"hidden", token=token) == "dropped"
                s.set_foreground(True)
                assert s.controller is controller and s.current_term is term
            elif boundary == "unmount":
                s.unbind_view()
                s.bind_view(term, write=render_write)
            else:
                await h.take_over()
            assert s.input_token != token and not s.armed_modifiers
            assert s.pending_review is None and not await s.confirm_paste()
            assert await old_keys.flush() == 0 and await s.flush_keys() == 0
            assert await s.handle_view_input(ORIGIN_TEXT, b"late", token=token) == "dropped"
            assert h.frames == []
            if boundary != "takeover":
                assert (len(h.ports.ops.attaches), len(h.ports.ops.leases)) == before
            assert s.notice == STALE_MESSAGE or boundary == "background"
            h.assert_draft_untouched()

    asyncio.run(scenario())


def test_uncertain_send_drops_remaining_batch_and_never_replays_after_takeover(
    session_type: SessionType, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async with connected(session_type) as h:
            s, socket = h.session, h.socket
            original_send = socket.send_bytes

            async def uncertain(data: bytes) -> None:
                await original_send(data)  # Delivery happened; response is lost.
                raise TransportError("host_offline", "unknown delivery", True)

            monkeypatch.setattr(socket, "send_bytes", uncertain)
            s.sessions.keys.commit_text("possibly delivered")
            s.sessions.keys.commit_text("remaining, never retry")
            with pytest.raises(TransportError):
                await s.flush_keys()
            assert [frame.data for frame in h.frames] == [b"possibly delivered"]
            assert await s.flush_keys() == 0
            await h.take_over()
            assert await s.flush_keys() == 0
            assert [frame.data for frame in h.frames] == [b"possibly delivered"]
            assert await s.type_text("new explicit input") == "pty"
            assert [frame.data for frame in h.frames] == [
                b"possibly delivered",
                b"new explicit input",
            ]
            assert [frame.input_seq for frame in h.frames] == [1, 1]
            assert h.frames[0].lease_id != h.frames[1].lease_id
            h.assert_draft_untouched()

    asyncio.run(scenario())
