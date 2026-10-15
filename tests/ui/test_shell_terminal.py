"""Standalone shell workflow + terminal UI mounts.

Headless: real `ShellSession` + `TerminalController` + `DetailSessions`
against `ScriptedShellGatewayOps` + scripted sockets, with Flet 1.0.3
components rendered via the host harness (no Flutter, no browser).

Covers the full shell scenario (create/open, type, select/copy, reviewed
paste, resize, disconnect, reconnect, explicit takeover, exit, close),
agent-surface mounting (ComposerBox + tools dock + paste review + input
modes, present and wired), absence of agent UI on the shell surface,
owner disclosure + takeover confirm, the reattach drive (reset doc ->
notice -> reattach -> exact-byte continuation), exit-vs-detach, and the
Send gate including the IME-composition block. Real-device/browser proof
stays a release-gate item (environment-blocked).
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any

import flet as ft
import httpx
import pytest

from relaydeck.client.adapters.fake import FakeClipboard
from relaydeck.client.features.composer.components import (
    COMPOSER_KEY,
    DRAFT_KEY,
    MARKERS_KEY,
    SEND_KEY,
)
from relaydeck.client.features.composer.domain import (
    INPUT_MODE_COMPOSER,
    INPUT_MODE_TOOLS_KEYS,
    LOST_DELIVERY_MESSAGE,
    MISSING_WRITER_MESSAGE,
    NO_BLOCKED_MESSAGE,
    REVIEW_LOST_MESSAGE,
    SEND_BLOCKED_REVIEW,
    SEND_DELIVERED_VIA_ATTACH,
    SEND_DELIVERED_VIA_PROMPT,
    SEND_FAILED,
    SEND_IGNORED,
    SUBMISSION_FAILED,
    SUBMISSION_SENDING,
    SUBMISSION_UNKNOWN,
    ComposerStore,
)
from relaydeck.client.features.shell import (
    CLOSE_MESSAGE,
    CLOSE_TITLE,
    FRESH_ATTACH_NOTICE,
    SHELL_CONFLICT,
    SHELL_DISCONNECTED,
    SHELL_EXITED,
    SHELL_LIVE,
    LeaseInfo,
    ScriptedShellGatewayOps,
    ShellOpError,
    ShellSession,
    ShellViewState,
)
from relaydeck.client.features.shell.screen import (
    SHELL_BACK_KEY,
    SHELL_CLOSE_CANCEL_BUTTON_KEY,
    SHELL_CLOSE_CONFIRM_BUTTON_KEY,
    SHELL_CLOSE_CONFIRM_KEY,
    SHELL_CLOSE_CONFIRM_MESSAGE_KEY,
    SHELL_CLOSE_CONFIRM_TITLE_KEY,
    SHELL_CLOSE_KEY,
    SHELL_CONFLICT_KEY,
    SHELL_CONFLICT_OWNER_KEY,
    SHELL_DISCONNECT_KEY,
    SHELL_DISCONNECTED_KEY,
    SHELL_EXITED_DETAIL_KEY,
    SHELL_EXITED_KEY,
    SHELL_INPUT_ROW_KEY,
    SHELL_MODE_KEYS_KEY,
    SHELL_MODE_TEXT_KEY,
    SHELL_NEW_TERMINAL_KEY,
    SHELL_NOTICE_KEY,
    SHELL_PASTE_KEY,
    SHELL_RECONNECT_KEY,
    SHELL_TAKEOVER_KEY,
    ShellScreen,
)
from relaydeck.client.features.sync.transport import TransportError
from relaydeck.client.features.terminal.agent_session import (
    AGENT_CONFLICT,
    AGENT_CONNECTING,
    AGENT_DISCONNECTED,
    AGENT_LIVE,
    TAKEOVER_UNDISCLOSED_MESSAGE,
)
from relaydeck.client.features.terminal.controller import TerminalController
from relaydeck.client.features.terminal.input_modes import (
    KEY_HOLD,
    KEYBOARD_KEYS,
    KEYBOARD_TEXT,
    TEXT_SEND,
    WORKFLOW_AGENT_COMPOSER,
    WORKFLOW_AGENT_DIRECT,
    WORKFLOW_SHELL,
    ImeState,
    InputModeState,
    route_committed_text,
)
from relaydeck.client.features.terminal.keys import (
    KEYS_DISMISS_KEY,
    KEYS_DOCK_KEY,
    MOD_CTRL,
    bind_keys_session,
    key_button_key,
    modifier_button_key,
)
from relaydeck.client.features.terminal.models import (
    TONE_WARN,
    NoticeModel,
    TerminalViewState,
    apply_live,
)
from relaydeck.client.features.terminal.paste_policy import (
    BRACKET_END,
    BRACKET_START,
    PasteReview,
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
    STALE_MESSAGE,
    TITLE_KEY,
)
from relaydeck.client.features.terminal.retention import RetentionFullError, TerminalRegistry
from relaydeck.client.features.terminal.selection_model import (
    close_selection,
    copy_range,
    mark_copied,
    open_selection,
    resolved_range,
)
from relaydeck.client.features.terminal.stream import INCOMPLETE_HISTORY
from relaydeck.client.features.terminal.surfaces import (
    AGENT_CONFLICT_KEY,
    AGENT_CONFLICT_OWNER_KEY,
    AGENT_DETAIL_KEY,
    AGENT_KEYBOARD_TEXT_KEY,
    AGENT_PASTE_ERROR_KEY,
    AGENT_STATUS_KEY,
    AGENT_TAKEOVER_CANCEL_KEY,
    AGENT_TAKEOVER_CONFIRM_KEY,
    AGENT_TAKEOVER_DISCLOSURE_KEY,
    AGENT_TAKEOVER_KEY,
    AGENT_TITLE_KEY,
    AGENT_WORKFLOW_COMPOSER_KEY,
    AGENT_WORKFLOW_DIRECT_KEY,
    DRIVE_IDLE,
    DRIVE_REATTACHED,
    DRIVE_REFUSED_EXITED,
    DRIVE_RESUMED,
    SEND_BLOCKED_IME,
    TAKEOVER_DISCLOSURE_AGENT,
    AgentTerminalScreen,
    DetailSessions,
    SnoopingTerminalSocket,
    drive_recovery,
    gated_composer_send,
    mount_agent_screen,
    offered_position,
)
from relaydeck.gateway import terminal_wire
from tests.support.host_harness import HostApp, button_label, render_component, run
from tests.support.terminal.sessions import (
    _agent_session,
    _ScriptedSocket,
    _session,
    _SocketFactory,
)


def _output(epoch: str, offset: int, data: bytes) -> tuple[str, Any]:
    return ("bytes", terminal_wire.encode_output(epoch, offset, data))


def _control(kind: str, detail: str = "") -> tuple[str, Any]:
    doc: dict[str, Any] = {"kind": kind}
    if detail:
        doc["detail"] = detail
    return ("text", json.dumps(doc))


async def _cancel(task: asyncio.Task[Any]) -> None:
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, TransportError):
        pass


# -- open: attach without takeover, control lease, stream ----------------------


def test_open_attaches_without_takeover_and_streams() -> None:
    async def scenario() -> None:
        session, ops, factory = _session()
        factory.queue(_output("e1", 0, b"$ "))
        assert await session.open("term-1") == SHELL_LIVE
        assert ops.attaches == [
            {
                "host_id": "home-mac",
                "terminal_id": "term-1",
                "kind": "shell",
                "takeover": False,
            }
        ]
        assert ops.leases == [{"attachment_id": "att-1", "mode": "control"}]
        assert ops.tickets == ["lease-1"]
        assert session.attachment is not None
        assert session.attachment.generation == 1
        assert session.lease is not None and session.lease.mode == "control"
        assert session.controller is not None
        assert session.controller.generation == 1
        assert session.sessions.bound
        assert session.sessions.modes.workflow == WORKFLOW_SHELL
        task = asyncio.ensure_future(session.run())
        for _ in range(200):
            if bytes(session.output_log) == b"$ ":
                break
            await asyncio.sleep(0.005)
        assert bytes(session.output_log) == b"$ "
        assert session.term_state.epoch == "e1"
        await _cancel(task)

    run(scenario())


def test_type_text_goes_to_pty_with_generation_and_seq() -> None:
    async def scenario() -> None:
        session, _, factory = _session()
        await session.open("term-1")
        assert await session.type_text("ls\n") == "pty"
        assert await session.type_text("pwd") == "pty"
        (sock,) = factory.sockets
        chunks = [terminal_wire.decode_input(frame) for frame in sock.sent_bytes]
        assert [(c.generation, c.input_seq, c.kind, c.data) for c in chunks] == [
            (1, 1, "text", b"ls\n"),
            (1, 2, "text", b"pwd"),
        ]
        assert all(c.lease_id == "lease-1" for c in chunks)

    run(scenario())


def test_press_key_and_modifiers_exact_bytes() -> None:
    async def scenario() -> None:
        session, _, factory = _session()
        await session.open("term-1")
        session.toggle_modifier(MOD_CTRL)
        assert await session.press_key("c") is True
        assert await session.press_key("left") is True
        assert session.sessions.keys.pending_modifiers == frozenset()
        (sock,) = factory.sockets
        datas = [terminal_wire.decode_input(frame).data for frame in sock.sent_bytes]
        assert datas == [b"\x03", b"\x1b[D"]
        # No live writer after disconnect: nothing queued, nothing sent.
        await session.disconnect()
        assert await session.press_key("a") is False
        assert [terminal_wire.decode_input(f).data for f in sock.sent_bytes] == [
            b"\x03",
            b"\x1b[D",
        ]

    run(scenario())


# -- selection + copy ------------------------------------------------------------


def test_select_copy_exact_and_never_inputs() -> None:
    async def scenario() -> None:
        clipboard = FakeClipboard()
        session, _, factory = _session(clipboard=clipboard)
        await session.open("term-1")
        session.update_term_state(open_selection(session.term_state, anchor=0, focus=5))
        text = "hello world"
        written = await session.copy_selection(text, 0, 5)
        assert written == "hello"
        assert clipboard.writes == ["hello"]
        marked = mark_copied(session.term_state, written)
        assert marked.selection.last_copied == "hello"
        session.update_term_state(close_selection(marked))
        assert session.term_state.selection.open is False
        (sock,) = factory.sockets
        assert sock.sent_bytes == []

    run(scenario())


def test_copy_unicode_range_byte_exact() -> None:
    async def scenario() -> None:
        clipboard = FakeClipboard()
        session, _, _ = _session(clipboard=clipboard)
        await session.open("term-1")
        text = "AB\u4e2d\u6587C\n\U0001f600 done"
        session.update_term_state(open_selection(session.term_state))
        written = await session.copy_selection(text, 0, len(text))
        assert written == text
        assert clipboard.writes[0].encode("utf-8") == text.encode("utf-8")

    run(scenario())


# -- reviewed paste -----------------------------------------------------------------


def test_reviewed_paste_confirm_sends_exact_framed_bytes() -> None:
    async def scenario() -> None:
        session, _, factory = _session()
        await session.open("term-1")
        session.set_bracketed_paste(True)
        assert session.bracketed_paste is True
        text = "printf 'a'\nprintf 'b'"
        assert await session.request_paste(text) == REQUEST_REVIEW
        review = session.pending_review
        assert review is not None
        assert (review.line_count, review.character_count) == (2, 21)
        assert review.preview == text
        (sock,) = factory.sockets
        assert sock.sent_bytes == []
        assert await session.confirm_paste() is True
        assert session.pending_review is None
        (frame,) = sock.sent_bytes
        chunk = terminal_wire.decode_input(frame)
        assert (chunk.generation, chunk.kind) == (1, "text")
        assert chunk.data == BRACKET_START + text.encode() + BRACKET_END

    run(scenario())


def test_paste_cancel_reject_and_insert_paths() -> None:
    async def scenario() -> None:
        session, _, factory = _session()
        await session.open("term-1")
        session.set_bracketed_paste(False)
        assert await session.request_paste("a\nb") == REQUEST_REVIEW
        session.cancel_paste()
        assert session.pending_review is None
        assert await session.confirm_paste() is False
        assert await session.request_paste("bad\x07") == REQUEST_REJECTED
        assert session.pending_review is None
        assert session.paste_error is not None and "U+0007" in session.paste_error
        assert await session.request_paste("ls") == REQUEST_INSERTED
        (sock,) = factory.sockets
        assert [terminal_wire.decode_input(f).data for f in sock.sent_bytes] == [b"ls"]

    run(scenario())


def test_paste_without_writer_blocks() -> None:
    async def scenario() -> None:
        session, _, factory = _session()
        await session.open("term-1")
        await session.disconnect()
        assert await session.request_paste("a\nb") == REQUEST_BLOCKED
        assert session.pending_review is None
        assert factory.sockets[0].sent_bytes == []

    run(scenario())


# -- resize ---------------------------------------------------------------------------


def test_control_resize_sends_and_view_rejects() -> None:
    async def scenario() -> None:
        session, _, factory = _session()
        await session.open("term-1")
        assert await session.resize(100, 30) is True
        (sock,) = factory.sockets
        (frame,) = sock.sent_bytes
        chunk = terminal_wire.decode_input(frame)
        assert (chunk.kind, chunk.cols, chunk.rows) == ("resize", 100, 30)
        # View leases never resize: controller-level gate.
        viewer = TerminalController(
            socket_factory=lambda ticket: _ScriptedSocket(),
            lease_id="lease-view",
            generation=1,
            mode="view",
            on_bytes=lambda data: None,
        )
        assert await viewer.request_resize(100, 30) is False
        assert viewer.rejected_resizes == [(100, 30)]

    run(scenario())


# -- disconnect / reconnect -------------------------------------------------------------


def test_disconnect_keeps_process_and_reconnect_self_takeovers() -> None:
    async def scenario() -> None:
        session, ops, factory = _session()
        await session.open("term-1")
        assert await session.type_text("echo hi\n") == "pty"
        assert await session.disconnect() == SHELL_DISCONNECTED
        assert ops.releases == ["lease-1"]
        assert session.lease is None
        assert not ops.is_closed("att-1")
        # Reconnect: attach refuses (self-owned) then self-takeover bumps.
        assert await session.reconnect() == SHELL_LIVE
        assert [a["takeover"] for a in ops.attaches] == [False, False, True]
        assert session.attachment is not None
        assert session.attachment.generation == 2
        assert session.attachment.attachment_id == "att-1"
        assert session.lease is not None and session.lease.generation == 2
        assert session.controller is not None
        assert session.controller.generation == 2
        assert session.notice != ""
        # New generation, seq restarts at 1; old lease id never reused.
        assert await session.type_text("x") == "pty"
        assert factory.sockets[-1] is not factory.sockets[0]
        chunks = [terminal_wire.decode_input(f) for f in factory.sockets[-1].sent_bytes]
        assert [(c.generation, c.input_seq) for c in chunks] == [(2, 1)]
        assert ops.tickets[-1] == session.lease.lease_id != "lease-1"

    run(scenario())


def test_reconnect_refused_when_exited() -> None:
    async def scenario() -> None:
        session, ops, _ = _session()
        await session.open("term-1")
        await session.close_terminal()
        tickets = list(ops.tickets)
        attaches = list(ops.attaches)
        assert await session.reconnect() == SHELL_EXITED
        assert await session.take_over() == SHELL_EXITED
        assert ops.tickets == tickets
        assert ops.attaches == attaches

    run(scenario())


def test_reconnect_after_close_elsewhere_opens_fresh_with_notice() -> None:
    """The pane ended elsewhere: reconnect re-attaches a NEW terminal
    (fresh id, generation 1) with an explicit notice, and the old bytes
    are dropped -- never presented as the new terminal's prefix."""

    async def scenario() -> None:
        session, ops, factory = _session()
        factory.queue(_output("e1", 0, b"old-bytes"))
        assert await session.open("term-1") == SHELL_LIVE
        task = asyncio.ensure_future(session.run())
        for _ in range(200):
            if bytes(session.output_log) == b"old-bytes":
                break
            await asyncio.sleep(0.005)
        assert bytes(session.output_log) == b"old-bytes"
        await _cancel(task)
        assert session.attachment is not None
        old_id = session.attachment.attachment_id
        assert await session.disconnect() == SHELL_DISCONNECTED
        # A second handle closes the pane on the Host.
        other = ScriptedShellGatewayOps(device_id="dev-2", label="dev 2")
        other._attachments = ops._attachments
        other._by_id = ops._by_id
        other._att_seq = ops._att_seq
        await other.close_attachment(old_id)
        factory.queue(_output("e1", 0, b"new"))
        assert await session.reconnect() == SHELL_LIVE
        assert session.attachment is not None
        assert session.attachment.attachment_id != old_id
        assert session.attachment.generation == 1
        assert FRESH_ATTACH_NOTICE == "Shell ended elsewhere; opened a fresh terminal."
        assert session.notice == FRESH_ATTACH_NOTICE
        assert bytes(session.output_log) == b""
        task = asyncio.ensure_future(session.run())
        for _ in range(200):
            if bytes(session.output_log) == b"new":
                break
            await asyncio.sleep(0.005)
        assert bytes(session.output_log) == b"new"
        await _cancel(task)

    run(scenario())


def test_reconnect_foreign_owner_lands_in_conflict_without_takeover() -> None:
    """Reconnect against a foreign-owned terminal: `conflict` with the
    disclosed owner label and zero takeover calls (explicit only)."""

    async def scenario() -> None:
        session, ops, _ = _session()
        await session.open("term-1")
        await session.disconnect()
        ops.seed_owner("home-mac", "term-1", "dev-9", "office mac")
        assert await session.reconnect() == SHELL_CONFLICT
        assert session.owner_label == "office mac"
        assert len(ops.attaches) == 2
        assert all(a["takeover"] is False for a in ops.attaches)

    run(scenario())


# -- conflict + explicit takeover ------------------------------------------------------------


def test_conflict_discloses_owner_and_takeover_is_explicit() -> None:
    async def scenario() -> None:
        ops = ScriptedShellGatewayOps()
        ops.seed_owner("home-mac", "term-9", "dev-9", "office mac")
        session, _, factory = _session(ops=ops)
        assert await session.open("term-9") == SHELL_CONFLICT
        assert session.owner_label == "office mac"
        assert session.controller is None
        assert session.lease is None
        assert factory.sockets == []
        assert await session.take_over() == SHELL_LIVE
        assert [a["takeover"] for a in ops.attaches] == [False, True]
        assert session.attachment is not None
        assert (session.attachment.generation, session.attachment.epoch) == (2, "e2")
        assert await session.type_text("ok\n") == "pty"
        chunk = terminal_wire.decode_input(factory.sockets[0].sent_bytes[0])
        assert (chunk.generation, chunk.input_seq) == (2, 1)

    run(scenario())


def test_takeover_drops_open_review_with_stale_notice() -> None:
    async def scenario() -> None:
        ops = ScriptedShellGatewayOps()
        session, _, _ = _session(ops=ops)
        await session.open("term-1")
        assert await session.request_paste("a\nb") == REQUEST_REVIEW
        assert session.pending_review is not None
        # Another device grabs ownership; our explicit take-over replaces
        # the writer while the review is open.
        other = ScriptedShellGatewayOps(device_id="dev-2", label="dev 2")
        other._attachments = ops._attachments
        other._by_id = ops._by_id
        other._att_seq = ops._att_seq
        await other.attach("home-mac", "term-1", "shell", True)
        assert await session.take_over() == SHELL_LIVE
        assert session.pending_review is None
        assert session.notice == STALE_MESSAGE
        assert session.attachment is not None and session.attachment.generation == 3

    run(scenario())


class _ControlHeldOps(ScriptedShellGatewayOps):
    """Scripted ops with a flippable foreign control holder: when armed,
    every control-lease acquisition conflicts (holder disclosure)."""

    hold_control: bool = False

    async def acquire_lease(self, attachment_id: str, mode: str) -> LeaseInfo:
        if mode == "control" and self.hold_control:
            self._control_holder[attachment_id] = ("acct-1", "dev-9")
        return await super().acquire_lease(attachment_id, mode)


def test_open_control_held_lands_in_conflict() -> None:
    async def scenario() -> None:
        ops = _ControlHeldOps()
        ops.hold_control = True
        session, _, factory = _session(ops=ops)
        assert await session.open("term-1") == SHELL_CONFLICT
        assert session.owner_label == "dev-9"
        assert session.controller is None
        assert session.lease is None
        assert factory.sockets == []
        assert ops.tickets == []

    run(scenario())


def test_takeover_control_held_lands_in_conflict() -> None:
    async def scenario() -> None:
        ops = _ControlHeldOps()
        session, _, _ = _session(ops=ops)
        await session.open("term-1")
        tickets = list(ops.tickets)
        ops.hold_control = True
        assert await session.take_over() == SHELL_CONFLICT
        assert session.owner_label == "dev-9"
        # The failed lease mints nothing; the old attachment is untouched.
        assert ops.tickets == tickets
        assert session.attachment is not None
        assert session.attachment.generation == 1

    run(scenario())


def test_reconnect_fresh_attach_control_held_lands_in_conflict() -> None:
    async def scenario() -> None:
        ops = _ControlHeldOps()
        session, _, _ = _session(ops=ops)
        await session.open("term-1")
        await session.disconnect()
        other = ScriptedShellGatewayOps(device_id="dev-2", label="dev 2")
        other._attachments = ops._attachments
        other._by_id = ops._by_id
        other._att_seq = ops._att_seq
        assert session.attachment is not None
        await other.close_attachment(session.attachment.attachment_id)
        ops.hold_control = True
        assert await session.reconnect() == SHELL_CONFLICT
        assert session.owner_label == "dev-9"
        assert all(a["takeover"] is False for a in ops.attaches)

    run(scenario())


def test_ticket_missing_is_typed_error_without_retryable() -> None:
    """A ticket-less 2xx is `upstream_incompatible`, never a KeyError;
    `ShellOpError` carries no `retryable` signal."""

    async def scenario() -> None:
        bodies = [{}, {"ticket": 123}, {"ticket": ""}]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=bodies.pop(0))

        async def token() -> str:
            return "tok"

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            from relaydeck.client.features.shell import HttpxShellGatewayOps

            ops = HttpxShellGatewayOps(base_url="http://t", token=token, client=client)
            for _ in range(3):
                with pytest.raises(ShellOpError) as exc:
                    await ops.terminal_ticket("lease-1")
                assert exc.value.code == "upstream_incompatible"
                assert not hasattr(exc.value, "retryable")
        assert not hasattr(ShellOpError("c", "m"), "retryable")

    run(scenario())


def test_agent_attach_requires_explicit_takeover() -> None:
    async def scenario() -> None:
        ops = ScriptedShellGatewayOps()
        with pytest.raises(ShellOpError) as exc:
            await ops.attach("home-mac", "term-a", "agent", False)
        assert exc.value.code == "forbidden"
        assert exc.value.details == {"reason": "agent_requires_takeover"}
        info = await ops.attach("home-mac", "term-a", "agent", True)
        assert info.takeover_authorized is True
        assert "ownership" in TAKEOVER_DISCLOSURE_AGENT

    run(scenario())


# -- exit semantics ------------------------------------------------------------------------------


def test_close_terminal_is_terminal_exit() -> None:
    async def scenario() -> None:
        session, ops, factory = _session()
        await session.open("term-1")
        assert session.view().confirm_close is False
        session.request_close()
        assert session.view().confirm_close is True
        session.cancel_close()
        assert session.view().confirm_close is False
        session.request_close()
        assert await session.close_terminal() == SHELL_EXITED
        assert [c["kind"] for c in ops.close_commands] == ["session.close"]
        assert ops.is_closed("att-1")
        assert session.lease is None
        assert session.view().confirm_close is False
        assert session.notice != ""
        # Dead lease never reused: the drive refuses without minting.
        tickets = list(ops.tickets)
        assert await session.drive_recovery() == DRIVE_REFUSED_EXITED
        assert ops.tickets == tickets
        # No auto-resurrect: input paths fail closed.
        assert await session.type_text("x") == "dropped"
        assert await session.request_paste("a\nb") == REQUEST_BLOCKED
        assert factory.sockets[0].sent_bytes == []

    run(scenario())


def test_bye_exited_marks_exit_while_reset_marks_disconnect() -> None:
    async def scenario() -> None:
        session, _, factory = _session()
        factory.queue(_control("bye", "exited"))
        await session.open("term-1")
        assert await session.run() == SHELL_EXITED
        assert session.lease is None
        assert session.notice != ""

        session2, _, factory2 = _session()
        factory2.queue(_control("reset", "stale_epoch"))
        await session2.open("term-1")
        assert await session2.run() == SHELL_DISCONNECTED
        assert session2.controller is not None
        assert session2.controller.reattach_needed is True
        assert session2.lease is not None

    run(scenario())


def test_exit_vs_detach_distinction() -> None:
    """Detach keeps the lease resumable-by-reconnect and the process alive;
    exit drops the lease and refuses every resume path."""

    async def scenario() -> None:
        session, ops, _ = _session()
        await session.open("term-1")
        await session.disconnect()
        assert session.phase == SHELL_DISCONNECTED
        assert not ops.is_closed("att-1")
        assert await session.reconnect() == SHELL_LIVE
        await session.close_terminal()
        assert session.phase == SHELL_EXITED
        assert ops.is_closed("att-1")
        assert session.lease is None
        assert await session.drive_recovery() == DRIVE_REFUSED_EXITED
        assert await session.reconnect() == SHELL_EXITED

    run(scenario())


# -- reattach drive -------------------------------------------------------------------------------


def test_reattach_drive_reset_notice_reattach_exact_continuation() -> None:
    async def scenario() -> None:
        session, ops, factory = _session()
        factory.queue(
            _output("e1", 0, b"hello"),
            _control("reset", "stale_epoch"),
        )
        await session.open("term-1")
        assert await session.run() == SHELL_DISCONNECTED
        assert bytes(session.output_log) == b"hello"
        controller = session.controller
        assert controller is not None
        assert controller.reattach_needed is True
        assert controller.pending_resume is None
        notice = controller.state.notice
        assert notice is not None and "incomplete" in notice.text.lower()
        # The shell mirrors the incomplete-history label at reset time.
        assert "incomplete" in session.notice.lower()
        # Drive 1: bare reattach -> fresh ticket, reopen at the live edge.
        factory.queue(_output("e1", 5, b" world"))
        assert await session.drive_recovery() == DRIVE_REATTACHED
        assert len(ops.tickets) == 2
        assert controller.reattach_needed is False
        assert await session.run() == SHELL_DISCONNECTED
        assert controller.pending_resume is not None
        # Drive 2: offered position -> exact-byte continuation.
        factory.queue(_output("e1", 5, b" world"))
        assert await session.drive_recovery() == DRIVE_RESUMED
        assert len(ops.tickets) == 3
        assert all(t == "lease-1" for t in ops.tickets)
        task = asyncio.ensure_future(session.run())
        for _ in range(200):
            if bytes(session.output_log) == b"hello world":
                break
            await asyncio.sleep(0.005)
        assert bytes(session.output_log) == b"hello world"
        await _cancel(task)
        # Recovery bytes re-apply live status with the incomplete-history
        # warning (the intermediate gap notice is superseded, not stacked):
        # the continued tail is never presented as complete.
        assert controller.state.status == "live"
        assert controller.state.notice is not None
        assert controller.state.notice.tone == "warning"
        assert controller.state.notice.text == INCOMPLETE_HISTORY
        assert controller.state.applied_offset == len(b"hello world")
        # The shell notice mirror refreshes on run()-exit classification;
        # the cancelled supervision task performs none, so it still holds
        # the earlier gap label (never a completeness claim).
        assert "Reconnecting" in session.notice

    run(scenario())


def test_drive_idle_without_flags_and_mint_failure_classifies() -> None:
    async def scenario() -> None:
        session, ops, _ = _session()
        await session.open("term-1")
        assert await session.drive_recovery() == DRIVE_IDLE
        assert len(ops.tickets) == 1
        # A dead lease fails the mint: classify, never resurrect.
        assert session.controller is not None
        from relaydeck.client.features.terminal.stream import ResumeRequest

        session.controller.pending_resume = ResumeRequest("e1", 5)
        await ops.release_lease("lease-1")
        assert await session.drive_recovery() == SHELL_DISCONNECTED
        assert session.controller.pending_resume is not None
        assert "reconnect" in session.notice.lower()

    run(scenario())


def test_drive_mints_before_close_so_failure_keeps_flags() -> None:
    async def scenario() -> None:
        session, ops, factory = _session()
        factory.queue(_control("reset", "stale_epoch"))
        await session.open("term-1")
        assert await session.run() == SHELL_DISCONNECTED
        controller = session.controller
        assert controller is not None and controller.reattach_needed is True
        await ops.release_lease("lease-1")
        with pytest.raises(ShellOpError) as exc:
            await drive_recovery(controller, ops.terminal_ticket)
        assert exc.value.code == "lease_expired"
        assert controller.reattach_needed is True
        assert len(factory.sockets) == 1

    run(scenario())


def test_offered_stream_position_tracks_client_gap() -> None:
    async def scenario() -> None:
        controller = _keys_controller()
        # Nothing offered before the stream opens.
        assert controller.offered_stream_position() == ("", None)
        assert offered_position(controller) == ("", None)
        sock = _ScriptedSocket(deque([_output("e1", 1000, b"late"), _control("bye")]))
        controller2 = TerminalController(
            socket_factory=lambda ticket: sock,
            lease_id="lease-1",
            generation=1,
            mode="control",
            on_bytes=lambda data: None,
        )
        await controller2.start("t1")
        await controller2.run()
        # The client-level gap offers the server retention base; the
        # public accessor reports it (this is the reattach target).
        assert controller2.offered_stream_position() == ("e1", 1000)
        assert offered_position(controller2) == ("e1", 1000)

    run(scenario())


def test_drive_failed_start_keeps_flags_actionable() -> None:
    async def scenario() -> None:
        session, _, _ = _session()
        await session.open("term-1")
        controller = session.controller
        assert controller is not None
        from relaydeck.client.features.terminal.stream import ResumeRequest

        controller.pending_resume = ResumeRequest("e1", 5)
        controller.reattach_needed = True

        async def mint(lease_id: str) -> str:
            return "ticket-2"

        async def boom(ticket: str, epoch: str = "", offset: int = 0) -> None:
            raise TransportError("host_offline", "reopen down")

        controller.start = boom  # type: ignore[method-assign]
        with pytest.raises(TransportError):
            await drive_recovery(controller, mint)
        assert controller.pending_resume == ResumeRequest("e1", 5)
        assert controller.reattach_needed is True

    run(scenario())


def test_snoop_records_last_control_without_mutating() -> None:
    async def scenario() -> None:
        inner = _ScriptedSocket(deque([_output("e1", 0, b"hi"), _control("reset", "stale_epoch")]))
        snoop = SnoopingTerminalSocket(inner)
        await snoop.connect()
        assert await snoop.receive() == _output("e1", 0, b"hi")
        assert await snoop.receive() == _control("reset", "stale_epoch")
        assert snoop.last_control == {"kind": "reset", "detail": "stale_epoch"}
        assert snoop.bytes_seen == len(terminal_wire.encode_output("e1", 0, b"hi"))
        assert snoop.texts_seen == 1

    run(scenario())


# -- DetailSessions mount contract ----------------------------------------------------------------


def _keys_controller(lease_id: str = "lease-1", generation: int = 1) -> TerminalController:
    sock = _ScriptedSocket()
    return TerminalController(
        socket_factory=lambda ticket: sock,
        lease_id=lease_id,
        generation=generation,
        mode="control",
        on_bytes=lambda data: None,
    )


def test_retarget_resets_modifiers_queue_tokens_deduper_ime() -> None:
    async def scenario() -> None:
        first = _keys_controller()
        await first.start("t1")
        sessions = DetailSessions(modes=InputModeState(workflow=WORKFLOW_SHELL))
        sessions.bind(first)
        sessions.keys.toggle_modifier(MOD_CTRL)
        assert sessions.keys.press_key("c") is not None
        assert len(sessions.keys.outbox) == 1
        token = sessions.keys.token
        assert sessions.deduper.key_down("k1", printable=True) == KEY_HOLD
        sessions.ime.begin()
        sessions.ime.update("ab")
        assert sessions.deduper.text(b"x") == TEXT_SEND

        second = _keys_controller(lease_id="lease-2", generation=2)
        await second.start("t2")
        assert sessions.retarget(second) is None
        assert sessions.keys.target_id == "lease-2"
        assert sessions.keys.pending_modifiers == frozenset()
        assert len(sessions.keys.outbox) == 0
        assert sessions.keys.token != token
        # Stale token callbacks emit nothing; deduper + IME reset alongside.
        assert sessions.keys.press_key("a", token=token) is None
        assert sessions.deduper.has_hold is False
        assert sessions.ime.is_composing is False
        assert sessions.ime.take_committed() is None
        assert sessions.submit_allowed() is True
        # Fresh emissions bind the new writer generation.
        assert sessions.keys.press_key("a") is not None
        assert await sessions.keys.flush() == 1

    run(scenario())


def test_takeover_resets_term_state_mirror() -> None:
    """Stale selection/notice must not survive a controller replacement."""

    async def scenario() -> None:
        session, _, _ = _session()
        await session.open("term-1")
        state = open_selection(session.term_state, anchor=0, focus=5)
        state = apply_live(state, "e1", 5, NoticeModel(TONE_WARN, "stale notice"))
        session.update_term_state(state)
        assert session.term_state.selection.open is True
        assert session.term_state.notice is not None
        assert await session.take_over() == SHELL_LIVE
        fresh = session.term_state
        assert fresh.selection.open is False
        assert fresh.notice is None
        # Post-takeover the replacement stream is not established yet:
        # connecting until the first chunk claims live (apply_live).
        assert fresh.status == "connecting"
        assert (fresh.epoch, fresh.applied_offset, fresh.lease_mode) == ("", 0, "control")

    run(scenario())


def test_has_live_writer_follows_writer_liveness() -> None:
    async def scenario() -> None:
        sessions = DetailSessions(modes=InputModeState(workflow=WORKFLOW_SHELL))
        assert sessions.has_live_writer() is False
        controller = _keys_controller()
        sessions.bind(controller)
        assert sessions.has_live_writer() is False  # socket never opened
        await controller.start("t1")
        assert sessions.has_live_writer() is True
        await controller.close()
        assert sessions.has_live_writer() is False
        live = _keys_controller(lease_id="lease-2", generation=2)
        await live.start("t2")
        sessions.retarget(live)
        assert sessions.has_live_writer() is True
        sessions.invalidate()
        assert sessions.has_live_writer() is False
        # View leases never hold a writer.
        viewer = TerminalController(
            socket_factory=lambda ticket: _ScriptedSocket(),
            lease_id="lease-view",
            generation=1,
            mode="view",
            on_bytes=lambda data: None,
        )
        await viewer.start("t3")
        sessions.bind(viewer)
        assert sessions.has_live_writer() is False

    run(scenario())


def test_shell_keys_enabled_follows_live_writer() -> None:
    async def scenario() -> None:
        session, _, _ = _session()
        assert session.keys_enabled is False  # unbound
        await session.open("term-1")
        assert session.keys_enabled is True
        await session.disconnect()
        assert session.keys_enabled is False
        await session.reconnect()
        assert session.keys_enabled is True
        await session.close_terminal()
        assert session.keys_enabled is False

    run(scenario())


def test_invalidate_drops_review_and_unbinds() -> None:
    async def scenario() -> None:
        controller = _keys_controller()
        await controller.start("t1")
        sessions = DetailSessions(modes=InputModeState(workflow=WORKFLOW_SHELL))
        sessions.bind(controller)
        assert await sessions.paste.request_paste("a\nb", True) == REQUEST_REVIEW
        assert sessions.invalidate() == STALE_MESSAGE
        assert sessions.bound is False
        with pytest.raises(RuntimeError):
            _ = sessions.keys

    run(scenario())


def test_shell_keyboard_mode_is_independent_and_draftless() -> None:
    async def scenario() -> None:
        session, _, factory = _session()
        await session.open("term-1")
        assert session.view().keyboard == KEYBOARD_TEXT
        assert await session.type_text("a") == "pty"
        # Keys mode: still PTY-routed, still draftless (no store, no
        # sync, no draft route), and the writer binding is untouched.
        token = session.sessions.keys.token
        session.set_keyboard(KEYBOARD_KEYS)
        assert session.view().keyboard == KEYBOARD_KEYS
        assert await session.type_text("b") == "pty"
        assert session.sessions.modes.store is None
        assert session.sessions.modes.sync_from_store() is False
        assert session.sessions.keys.token == token
        session.set_keyboard(KEYBOARD_TEXT)
        assert session.view().keyboard == KEYBOARD_TEXT
        assert await session.type_text("c") == "pty"
        (sock,) = factory.sockets
        datas = [terminal_wire.decode_input(f).data for f in sock.sent_bytes]
        assert datas == [b"a", b"b", b"c"]

    run(scenario())


def test_shell_routing_never_touches_a_draft() -> None:
    async def scenario() -> None:
        controller = _keys_controller()
        await controller.start("t1")
        from relaydeck.client.features.terminal.input_modes import ROUTE_PTY

        modes = InputModeState(workflow=WORKFLOW_SHELL)
        keys = bind_keys_session(controller)
        assert route_committed_text(modes, store=None, session=keys, text="echo hi") == ROUTE_PTY
        assert await keys.flush() == 1

    run(scenario())


# -- Send gating ----------------------------------------------------------------------------------


def _composer(**kwargs: Any) -> tuple[ComposerStore, list[dict[str, Any]]]:
    submitted: list[dict[str, Any]] = []

    async def submit(command: dict[str, Any]) -> dict[str, Any]:
        submitted.append(command)
        return {"command_id": command["command_id"]}

    async def prompt(target: str, text: str) -> dict[str, Any]:
        return {"ok": True}

    store = ComposerStore(
        target="sess:1",
        submit_command=submit,
        prompt_text=prompt,
        host_id="home-mac",
        host_epoch="epoch-1",
        agent_id="agent-1",
        **kwargs,
    )
    return store, submitted


def test_send_gated_on_ime_composition() -> None:
    async def scenario() -> None:
        store, submitted = _composer()
        store.replace_draft("hello")
        ime = ImeState()
        ime.begin()
        ime.update("composing")
        assert await gated_composer_send(store, ime) == SEND_BLOCKED_IME
        assert submitted == []
        assert store.draft == "hello"
        assert store.messages == ()
        ime.commit("done")
        assert ime.take_committed() == "done"
        assert await gated_composer_send(store, ime) == SEND_DELIVERED_VIA_PROMPT
        assert len(submitted) == 1

    run(scenario())


def test_detail_submit_allowed_tracks_ime_lifecycle() -> None:
    async def scenario() -> None:
        sessions = DetailSessions(modes=InputModeState(workflow=WORKFLOW_AGENT_COMPOSER))
        assert sessions.submit_allowed() is True
        sessions.ime.begin()
        assert sessions.submit_allowed() is False
        sessions.ime.update("marked")
        assert sessions.submit_allowed() is False
        sessions.ime.commit("done")
        assert sessions.ime.take_committed() == "done"
        assert sessions.submit_allowed() is True
        # Cancel and reset both release the gate.
        sessions.ime.begin()
        sessions.ime.cancel()
        assert sessions.submit_allowed() is True
        sessions.ime.begin()
        sessions.ime.reset()
        assert sessions.submit_allowed() is True
        # The gate agrees with the composer call-site guard end to end.
        store, submitted = _composer()
        store.replace_draft("hello")
        sessions.ime.begin()
        assert await gated_composer_send(store, sessions.ime) == SEND_BLOCKED_IME
        assert submitted == []
        sessions.ime.commit("x")
        assert sessions.ime.take_committed() == "x"
        assert sessions.submit_allowed() is True
        assert await gated_composer_send(store, sessions.ime) == SEND_DELIVERED_VIA_PROMPT
        assert len(submitted) == 1

    run(scenario())


# -- shell screen rendering -----------------------------------------------------------------------


def _shell_callbacks() -> dict[str, Any]:
    calls: dict[str, Any] = {
        "jump": 0,
        "size": [],
        "input": [],
        "selection": [],
        "copy": [],
        "close_selection": 0,
        "paste": 0,
        "mode": [],
        "key": [],
        "mod": [],
        "dismiss": 0,
        "confirm_paste": 0,
        "cancel_paste": 0,
        "disconnect": 0,
        "reconnect": 0,
        "takeover": 0,
        "back": 0,
        "close": 0,
        "confirm_close": 0,
        "cancel_close": 0,
        "new_terminal": 0,
    }
    return calls


def _render_shell(
    view: ShellViewState,
    terminal: TerminalViewState,
    calls: dict[str, Any],
    review: PasteReview | None = None,
    paste_error: str | None = None,
    armed: frozenset[str] = frozenset(),
    keys_enabled: bool = True,
    selection_text: str = "",
) -> Any:
    return render_component(
        ShellScreen,
        view=view,
        terminal=terminal,
        selection_text=selection_text,
        review=review,
        paste_error=paste_error,
        armed=armed,
        keys_enabled=keys_enabled,
        on_jump_to_bottom=lambda: calls.update(jump=calls["jump"] + 1),
        on_size_report=lambda c, r: calls["size"].append((c, r)),
        on_input=lambda o, d: calls["input"].append((o, d)),
        on_open_selection=lambda: calls["selection"].append(True),
        on_copy_selection=lambda a, f: calls["copy"].append((a, f)),
        on_close_selection=lambda: calls.update(close_selection=calls["close_selection"] + 1),
        on_paste=lambda: calls.update(paste=calls["paste"] + 1),
        on_mode_change=lambda m: calls["mode"].append(m),
        on_key=lambda name: calls["key"].append(name),
        on_toggle_modifier=lambda m: calls["mod"].append(m),
        on_dismiss_keys=lambda: calls.update(dismiss=calls["dismiss"] + 1),
        on_confirm_paste=lambda: calls.update(confirm_paste=calls["confirm_paste"] + 1),
        on_cancel_paste=lambda: calls.update(cancel_paste=calls["cancel_paste"] + 1),
        on_disconnect=lambda: calls.update(disconnect=calls["disconnect"] + 1),
        on_reconnect=lambda: calls.update(reconnect=calls["reconnect"] + 1),
        on_takeover=lambda: calls.update(takeover=calls["takeover"] + 1),
        on_back=lambda: calls.update(back=calls["back"] + 1),
        on_close_terminal=lambda: calls.update(close=calls["close"] + 1),
        on_confirm_close=lambda: calls.update(confirm_close=calls["confirm_close"] + 1),
        on_cancel_close=lambda: calls.update(cancel_close=calls["cancel_close"] + 1),
        on_new_terminal=lambda: calls.update(new_terminal=calls["new_terminal"] + 1),
    )


def _absent(app: HostApp, key: str) -> None:
    try:
        app.find(key)
    except KeyError:
        return
    raise AssertionError(f"{key} must be absent")


def test_shell_screen_live_renders_input_row_and_wires_callbacks() -> None:
    async def scenario() -> None:
        calls = _shell_callbacks()
        view = ShellViewState(phase=SHELL_LIVE, terminal_id="term-1", keyboard="text")
        app = await _render_shell(view, TerminalViewState(lease_mode="control"), calls)
        assert app.find(SHELL_INPUT_ROW_KEY) is not None
        assert button_label(app.find(SHELL_PASTE_KEY)) == "Paste"
        # One More keys toggle replaces the Text/Keys pair;
        # with the text keyboard showing it offers the tools dock.
        _absent(app, SHELL_MODE_TEXT_KEY)
        assert app.find(SHELL_MODE_KEYS_KEY).disabled is False
        assert button_label(app.find(SHELL_DISCONNECT_KEY)) == "Release"
        assert app.find(SHELL_BACK_KEY) is not None
        # Close Terminal lives in the More menu (with a confirmation).
        assert button_label(app.find(SHELL_CLOSE_KEY)) == "Close terminal…"
        assert app.find("terminal-surface") is not None
        assert button_label(app.find("terminal-select-text")) == "Select text"
        _absent(app, KEYS_DOCK_KEY)
        await app.set_props_and_fire(SHELL_PASTE_KEY, {}, "click")
        await app.set_props_and_fire(SHELL_MODE_KEYS_KEY, {}, "click")
        await app.set_props_and_fire(SHELL_DISCONNECT_KEY, {}, "click")
        await app.set_props_and_fire(SHELL_BACK_KEY, {}, "click")
        await app.set_props_and_fire(SHELL_CLOSE_KEY, {}, "click")
        assert calls["paste"] == 1
        assert calls["mode"] == [KEYBOARD_KEYS]
        assert calls["disconnect"] == 1
        assert calls["back"] == 1
        assert calls["close"] == 1

    run(scenario())


def test_shell_screen_keys_mode_mounts_dock() -> None:
    async def scenario() -> None:
        calls = _shell_callbacks()
        view = ShellViewState(phase=SHELL_LIVE, terminal_id="term-1", keyboard="keys")
        app = await _render_shell(
            view, TerminalViewState(lease_mode="control"), calls, armed=frozenset({MOD_CTRL})
        )
        assert app.find(KEYS_DOCK_KEY) is not None
        assert app.find("tools-grid-scroll").scroll == ft.ScrollMode.AUTO
        assert app.find(modifier_button_key(MOD_CTRL)).content == "\u25cf Ctrl"
        await app.set_props_and_fire(key_button_key("escape"), {}, "click")
        await app.set_props_and_fire(modifier_button_key("alt"), {}, "click")
        await app.set_props_and_fire(KEYS_DISMISS_KEY, {}, "click")
        assert calls["key"] == ["escape"]
        assert calls["mod"] == ["alt"]
        assert calls["dismiss"] == 1

    run(scenario())


def test_shell_screen_has_no_agent_ui() -> None:
    """no agent composer, agent switcher, staged state, agent
    notification controls, or agent status on the shell surface."""

    async def scenario() -> None:
        calls = _shell_callbacks()
        review = PasteReview(line_count=2, character_count=3, preview="a\nb")
        for phase in (SHELL_LIVE, SHELL_CONFLICT, SHELL_DISCONNECTED, SHELL_EXITED):
            view = ShellViewState(
                phase=phase, terminal_id="term-1", keyboard="keys", owner_label="x"
            )
            app = await _render_shell(
                view, TerminalViewState(lease_mode="control"), calls, review=review
            )
            assert app.find("terminal-surface") is not None
            for key in (
                COMPOSER_KEY,
                SEND_KEY,
                DRAFT_KEY,
                MARKERS_KEY,
                "composer-blocked-review",
                "composer-mode",
                "agent-detail",
                "agent-detail-title",
                "agent-detail-status",
                "agent-workflow-row",
                "agent-workflow-composer",
                "agent-workflow-direct",
            ):
                _absent(app, key)

    run(scenario())


def test_shell_conflict_renders_owner_and_takeover() -> None:
    async def scenario() -> None:
        calls = _shell_callbacks()
        view = ShellViewState(phase=SHELL_CONFLICT, terminal_id="term-9", owner_label="office mac")
        app = await _render_shell(view, TerminalViewState(), calls)
        assert app.find(SHELL_CONFLICT_KEY) is not None
        assert app.find(SHELL_CONFLICT_OWNER_KEY).value == "Terminal owned by office mac"
        assert button_label(app.find(SHELL_TAKEOVER_KEY)) == "Take over"
        _absent(app, SHELL_INPUT_ROW_KEY)
        _absent(app, SHELL_RECONNECT_KEY)
        await app.set_props_and_fire(SHELL_TAKEOVER_KEY, {}, "click")
        await app.set_props_and_fire(SHELL_BACK_KEY, {}, "click")
        assert calls["takeover"] == 1
        assert calls["back"] == 1

    run(scenario())


def test_shell_disconnected_offers_reattach_only() -> None:
    async def scenario() -> None:
        calls = _shell_callbacks()
        view = ShellViewState(phase=SHELL_DISCONNECTED, terminal_id="term-1", notice="dropped")
        app = await _render_shell(view, TerminalViewState(), calls)
        assert app.find(SHELL_DISCONNECTED_KEY) is not None
        assert button_label(app.find(SHELL_RECONNECT_KEY)) == "Reattach"
        assert app.find(SHELL_NOTICE_KEY).value == "dropped"
        _absent(app, SHELL_INPUT_ROW_KEY)
        _absent(app, SHELL_TAKEOVER_KEY)
        await app.set_props_and_fire(SHELL_RECONNECT_KEY, {}, "click")
        assert calls["reconnect"] == 1

    run(scenario())


def test_shell_exited_offers_new_terminal_only() -> None:
    async def scenario() -> None:
        calls = _shell_callbacks()
        view = ShellViewState(phase=SHELL_EXITED, terminal_id="term-1")
        app = await _render_shell(view, TerminalViewState(), calls)
        assert app.find(SHELL_EXITED_KEY) is not None
        assert app.find(SHELL_EXITED_DETAIL_KEY).value == "Shell exited."
        assert button_label(app.find(SHELL_NEW_TERMINAL_KEY)) == "New terminal"
        # No resurrect affordances: no reattach, disconnect, or takeover.
        _absent(app, SHELL_INPUT_ROW_KEY)
        _absent(app, SHELL_RECONNECT_KEY)
        _absent(app, SHELL_DISCONNECT_KEY)
        _absent(app, SHELL_TAKEOVER_KEY)
        await app.set_props_and_fire(SHELL_NEW_TERMINAL_KEY, {}, "click")
        assert calls["new_terminal"] == 1

    run(scenario())


def test_shell_close_confirm_flow() -> None:
    async def scenario() -> None:
        calls = _shell_callbacks()
        view = ShellViewState(phase=SHELL_LIVE, terminal_id="term-1", confirm_close=True)
        app = await _render_shell(view, TerminalViewState(lease_mode="control"), calls)
        assert app.find(SHELL_CLOSE_CONFIRM_KEY) is not None
        assert app.find(SHELL_CLOSE_CONFIRM_TITLE_KEY).value == CLOSE_TITLE
        assert app.find(SHELL_CLOSE_CONFIRM_MESSAGE_KEY).value == CLOSE_MESSAGE
        await app.set_props_and_fire(SHELL_CLOSE_CONFIRM_BUTTON_KEY, {}, "click")
        await app.set_props_and_fire(SHELL_CLOSE_CANCEL_BUTTON_KEY, {}, "click")
        assert calls["confirm_close"] == 1
        assert calls["cancel_close"] == 1

        plain = await _render_shell(
            ShellViewState(phase=SHELL_LIVE, terminal_id="term-1"),
            TerminalViewState(lease_mode="control"),
            _shell_callbacks(),
        )
        _absent(plain, SHELL_CLOSE_CONFIRM_KEY)

    run(scenario())


def test_shell_paste_review_mount() -> None:
    async def scenario() -> None:
        calls = _shell_callbacks()
        review = PasteReview(line_count=2, character_count=6, preview="a\nb\tc")
        view = ShellViewState(phase=SHELL_LIVE, terminal_id="term-1")
        app = await _render_shell(
            view, TerminalViewState(lease_mode="control"), calls, review=review
        )
        assert app.find(SHEET_KEY) is not None
        assert app.find(TITLE_KEY).value == "Review paste"
        assert app.find(COUNTS_KEY).value == "2 lines \u00b7 6 characters"
        assert app.find(PREVIEW_KEY).value == "a\nb\tc"
        await app.set_props_and_fire(CONFIRM_KEY, {}, "click")
        await app.set_props_and_fire(CANCEL_KEY, {}, "click")
        assert calls["confirm_paste"] == 1
        assert calls["cancel_paste"] == 1

        blocked = await _render_shell(
            view,
            TerminalViewState(lease_mode="control"),
            _shell_callbacks(),
            paste_error="Terminal is not accepting input.",
        )
        assert blocked.find("shell-paste-error").value == "Terminal is not accepting input."

    run(scenario())


def test_shell_selection_sheet_mount() -> None:
    async def scenario() -> None:
        calls = _shell_callbacks()
        state = open_selection(TerminalViewState(lease_mode="control"), anchor=0, focus=5)
        view = ShellViewState(phase=SHELL_LIVE, terminal_id="term-1")
        app = await _render_shell(view, state, calls, selection_text="hello world")
        assert app.find("select-text-sheet") is not None
        await app.set_props_and_fire("select-text-copy", {}, "click")
        await app.set_props_and_fire("select-text-close", {}, "click")
        assert calls["copy"] == [(0, 5)]
        assert calls["close_selection"] == 1

    run(scenario())


# -- agent surface mounting -----------------------------------------------------------------------


def _agent_store() -> ComposerStore:
    store, _ = _composer()
    store.replace_draft("hello agent")
    return store


def _render_agent(
    calls: dict[str, Any],
    store: ComposerStore,
    workflow: str = WORKFLOW_AGENT_COMPOSER,
    keyboard: str = KEYBOARD_TEXT,
    paste_review: PasteReview | None = None,
    paste_error: str | None = None,
    terminal: TerminalViewState | None = None,
    agent_status: str = "Working",
    conflict_owner: str | None = None,
    takeover_disclosure: str | None = None,
    armed: frozenset[str] = frozenset(),
    keys_enabled: bool = True,
    blocked_review: PasteReview | None = None,
) -> Any:
    return render_component(
        AgentTerminalScreen,
        terminal=terminal or TerminalViewState(lease_mode="control"),
        selection_text="",
        agent_title="agent-1",
        agent_status=agent_status,
        workflow=workflow,
        keyboard=keyboard,
        draft=store.draft,
        markers=store.markers,
        can_send=store.can_send,
        blockers=store.send_blockers(),
        submission=store.submission,
        error=store.error,
        blocked_review=blocked_review,
        input_mode=store.input_mode,
        paste_review=paste_review,
        paste_error=paste_error,
        armed=armed,
        keys_enabled=keys_enabled,
        conflict_owner=conflict_owner,
        takeover_disclosure=takeover_disclosure,
        on_jump_to_bottom=lambda: calls.update(jump=True),
        on_size_report=lambda c, r: calls.update(size=(c, r)),
        on_input=lambda o, d: calls.update(input=(o, d)),
        on_open_selection=lambda: calls.update(open_selection=True),
        on_copy_selection=lambda a, f: calls.update(copy=(a, f)),
        on_close_selection=lambda: calls.update(close_selection=True),
        on_draft_change=lambda text: calls.update(draft=text),
        on_send=lambda: calls.update(send=True),
        on_confirm_blocked=lambda: calls.update(confirm_blocked=True),
        on_cancel_blocked=lambda: calls.update(cancel_blocked=True),
        on_clear_error=lambda: calls.update(clear_error=True),
        on_workflow_change=lambda w: calls.update(workflow=w),
        on_keyboard_change=lambda k: calls.update(keyboard=k),
        on_key=lambda name: calls.update(key=name),
        on_toggle_modifier=lambda m: calls.update(mod=m),
        on_dismiss_keys=lambda: calls.update(dismiss=True),
        on_confirm_paste=lambda: calls.update(confirm_paste=True),
        on_cancel_paste=lambda: calls.update(cancel_paste=True),
        on_takeover=lambda: calls.update(takeover=True),
        on_confirm_takeover=lambda: calls.update(confirm_takeover=True),
        on_cancel_takeover=lambda: calls.update(cancel_takeover=True),
    )


def test_agent_mounts_composer_tools_paste_and_modes() -> None:
    async def scenario() -> None:
        calls: dict[str, Any] = {}
        store = _agent_store()
        review = PasteReview(line_count=2, character_count=3, preview="a\nb")
        app = await _render_agent(
            calls,
            store,
            keyboard=KEYBOARD_KEYS,
            paste_review=review,
            armed=frozenset({MOD_CTRL}),
        )
        assert app.find(AGENT_DETAIL_KEY) is not None
        assert app.find(AGENT_TITLE_KEY).value == "agent-1"
        assert app.find(AGENT_STATUS_KEY).value == "Working"
        assert app.find("terminal-surface") is not None
        # ComposerBox contract: draft + send + five callbacks.
        assert app.find(COMPOSER_KEY) is not None
        assert app.find(DRAFT_KEY).value == "hello agent"
        assert app.find(SEND_KEY).disabled is False
        await app.set_props_and_fire(DRAFT_KEY, {"value": "edited"}, "change", "edited")
        await app.set_props_and_fire(SEND_KEY, {}, "click")
        assert calls["draft"] == "edited"
        assert calls["send"] is True
        # Workflow + keyboard switches.
        assert app.find(AGENT_WORKFLOW_COMPOSER_KEY).disabled is True
        assert app.find(AGENT_WORKFLOW_DIRECT_KEY).disabled is False
        await app.set_props_and_fire(AGENT_WORKFLOW_DIRECT_KEY, {}, "click")
        await app.set_props_and_fire(AGENT_KEYBOARD_TEXT_KEY, {}, "click")
        assert calls["workflow"] == WORKFLOW_AGENT_DIRECT
        assert calls["keyboard"] == KEYBOARD_TEXT
        # Tools dock contract: armed/enabled + key/modifier/dismiss.
        # item 1: Enter lives in the inline row now; the dock
        # routes its own deduped keys through the same callback.
        assert app.find(KEYS_DOCK_KEY) is not None
        await app.set_props_and_fire("shortcut-key-home", {}, "click")
        await app.set_props_and_fire("tools-page-terminal", {}, "click")
        await app.set_props_and_fire(modifier_button_key(MOD_CTRL), {}, "click")
        await app.set_props_and_fire(KEYS_DISMISS_KEY, {}, "click")
        assert calls["key"] == "home"
        assert calls["mod"] == MOD_CTRL
        assert calls["dismiss"] is True
        # Paste review contract: keys/counts/preview + confirm/cancel.
        assert app.find(SHEET_KEY) is not None
        assert app.find(COUNTS_KEY).value == "2 lines \u00b7 3 characters"
        assert app.find(PREVIEW_KEY).value == "a\nb"
        await app.set_props_and_fire(CONFIRM_KEY, {}, "click")
        await app.set_props_and_fire(CANCEL_KEY, {}, "click")
        assert calls["confirm_paste"] is True
        assert calls["cancel_paste"] is True

    run(scenario())


def test_agent_direct_hides_composer_keeps_tools() -> None:
    async def scenario() -> None:
        app = await _render_agent(
            {}, _agent_store(), workflow=WORKFLOW_AGENT_DIRECT, keyboard=KEYBOARD_KEYS
        )
        _absent(app, COMPOSER_KEY)
        _absent(app, SEND_KEY)
        assert app.find(KEYS_DOCK_KEY) is not None
        assert app.find("terminal-surface") is not None

    run(scenario())


def test_agent_blocked_review_and_error_paths_wired() -> None:
    async def scenario() -> None:
        calls: dict[str, Any] = {}
        store = _agent_store()
        review = PasteReview(line_count=2, character_count=3, preview="x\ny")
        app = await _render_agent(calls, store, blocked_review=review)
        assert app.find("composer-blocked-review") is not None
        assert app.find("composer-blocked-preview").value == "x\ny"
        await app.set_props_and_fire("composer-blocked-confirm", {}, "click")
        await app.set_props_and_fire("composer-blocked-cancel", {}, "click")
        assert calls["confirm_blocked"] is True
        assert calls["cancel_blocked"] is True

    run(scenario())


def test_agent_status_is_read_only_metadata() -> None:
    """Agent status renders the metadata value verbatim across stream
    states; stream end/reset never infers a new status."""

    async def scenario() -> None:
        tricky = "Approve? [y/n] -- do NOT auto-respond"
        for stream in (
            TerminalViewState(lease_mode="control"),
            apply_live(TerminalViewState(lease_mode="control"), "e9", 10),
        ):
            app = await _render_agent({}, _agent_store(), terminal=stream, agent_status=tricky)
            assert app.find(AGENT_STATUS_KEY).value == tricky
        done = await _render_agent({}, _agent_store(), agent_status="Done")
        assert done.find(AGENT_STATUS_KEY).value == "Done"

    run(scenario())


def test_agent_takeover_discloses_ownership_before_confirm() -> None:
    async def scenario() -> None:
        calls: dict[str, Any] = {}
        app = await _render_agent(
            calls,
            _agent_store(),
            conflict_owner="office mac",
            takeover_disclosure=TAKEOVER_DISCLOSURE_AGENT,
        )
        assert app.find(AGENT_CONFLICT_KEY) is not None
        assert app.find(AGENT_CONFLICT_OWNER_KEY).value == "Terminal owned by office mac"
        # With the disclosure armed, Take over IS the confirm step.
        assert button_label(app.find(AGENT_TAKEOVER_CONFIRM_KEY)) == "Take over"
        assert app.find(AGENT_TAKEOVER_DISCLOSURE_KEY) is not None
        _absent(app, AGENT_TAKEOVER_KEY)
        conflict_only = await _render_agent(calls, _agent_store(), conflict_owner="office mac")
        assert button_label(conflict_only.find(AGENT_TAKEOVER_KEY)) == "Take over"
        _absent(conflict_only, AGENT_TAKEOVER_CONFIRM_KEY)
        await conflict_only.set_props_and_fire(AGENT_TAKEOVER_KEY, {}, "click")
        await app.set_props_and_fire(AGENT_TAKEOVER_CONFIRM_KEY, {}, "click")
        await app.set_props_and_fire(AGENT_TAKEOVER_CANCEL_KEY, {}, "click")
        assert calls["takeover"] is True
        assert calls["confirm_takeover"] is True
        assert calls["cancel_takeover"] is True

    run(scenario())


def test_agent_paste_error_renders() -> None:
    async def scenario() -> None:
        app = await _render_agent({}, _agent_store(), paste_error="Paste blocked: no writer.")
        assert app.find(AGENT_PASTE_ERROR_KEY).value == "Paste blocked: no writer."

    run(scenario())


def test_copy_range_pure_unit_byte_exact() -> None:
    """Pure selection-model unit (no agent surface, no session): the
    anchored range resolves and copies byte-exactly to the clipboard."""

    async def scenario() -> None:
        clipboard = FakeClipboard()
        text = "line1\nline2"
        state = open_selection(TerminalViewState(lease_mode="control"))
        written = await copy_range(text, resolved_range(state, text), clipboard)
        assert written == text
        assert clipboard.writes == [text]

    run(scenario())


# -- agent session wiring (real ComposerStore, no calls-dict) -----------------


def test_agent_wired_send_delivers_prompt_through_real_store() -> None:
    async def scenario() -> None:
        session, ops, factory, submitted = _agent_session()
        # Open arms the disclosure and performs NO attach.
        assert session.open() == AGENT_CONNECTING
        assert session.takeover_disclosure == TAKEOVER_DISCLOSURE_AGENT
        assert ops.attaches == []
        assert await session.confirm_takeover() == AGENT_LIVE
        assert [a["takeover"] for a in ops.attaches] == [True]
        assert session.takeover_disclosure is None
        assert session.keys_enabled is True
        callbacks = session.composer_callbacks()
        assert sorted(callbacks) == [
            "on_cancel_blocked",
            "on_clear_error",
            "on_confirm_blocked",
            "on_draft_change",
            "on_send",
        ]
        callbacks["on_draft_change"]("hello agent")
        assert session.store.draft == "hello agent"
        assert session.composer_props()["draft"] == "hello agent"
        assert session.composer_props()["can_send"] is True
        assert await callbacks["on_send"]() == SEND_DELIVERED_VIA_PROMPT
        assert len(submitted) == 1
        assert submitted[0]["kind"] == "agent.prompt"
        assert submitted[0]["target"] == {"agent_id": "agent-1"}
        assert submitted[0]["payload"] == {"text": "hello agent"}
        assert session.store.draft == ""  # cleared only by successful delivery
        # Empty draft cannot send (validation, no transport).
        assert await callbacks["on_send"]() == SEND_IGNORED
        assert len(submitted) == 1
        # IME composition blocks with the store untouched (no convention path).
        messages_before = len(session.store.messages)
        session.on_draft_change("gated")
        session.sessions.ime.begin()
        assert session.submit_allowed() is False
        assert await session.on_send() == SEND_BLOCKED_IME
        assert len(submitted) == 1
        assert len(session.store.messages) == messages_before
        assert session.store.draft == "gated"
        session.sessions.ime.commit("gated")
        assert session.sessions.ime.take_committed() == "gated"
        assert await session.on_send() == SEND_DELIVERED_VIA_PROMPT
        assert len(submitted) == 2
        # Workflow switches preserve the hidden draft; Direct types to the PTY.
        session.on_draft_change("unsent draft")
        session.set_workflow(WORKFLOW_AGENT_DIRECT)
        assert session.store.draft == "unsent draft"
        assert await session.type_text("ls\n") == "pty"
        session.set_workflow(WORKFLOW_AGENT_COMPOSER)
        assert session.store.draft == "unsent draft"
        (sock,) = factory.sockets
        assert [terminal_wire.decode_input(f).data for f in sock.sent_bytes] == [b"ls\n"]

    run(scenario())


def test_agent_wired_callbacks_route_to_real_store_methods() -> None:
    async def scenario() -> None:
        session, _, _, _ = _agent_session()
        session.open()
        await session.confirm_takeover()
        callbacks = session.composer_callbacks()
        callbacks["on_draft_change"]("one")
        assert session.store.draft == "one"

        class _NotFound(Exception):
            code = "agent_not_found"

        async def failing_prompt(target: str, text: str) -> dict[str, Any]:
            raise _NotFound("gone")

        failing, _, _, _ = _agent_session(prompt=failing_prompt)
        failing.open()
        await failing.confirm_takeover()
        failing.on_draft_change("hello")
        assert await failing.on_send() == SEND_FAILED
        assert failing.store.error is not None
        assert "herdr rejected the message" in failing.store.error
        assert failing.store.draft == "hello"  # failure keeps the draft
        failing.on_clear_error()
        assert failing.store.error is None
        # Blocked confirm/cancel with no pending review hit the real store.
        assert await failing.on_confirm_blocked() is False
        assert failing.store.error == NO_BLOCKED_MESSAGE
        failing.on_clear_error()
        assert failing.on_cancel_blocked() is False
        assert failing.store.pending_blocked is None

    run(scenario())


def test_agent_blocked_flow_inserts_raw_and_latches_tools_keys() -> None:
    async def scenario() -> None:
        session, _, factory, submitted = _agent_session(initial_status="blocked")
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        assert session.view().keyboard == KEYBOARD_TEXT
        # Single-line blocked send inserts raw UTF-8 (no Enter, no framing).
        session.on_draft_change("y")
        assert await session.on_send() == SEND_DELIVERED_VIA_ATTACH
        assert submitted == []  # blocked path skips the RPC
        (sock,) = factory.sockets
        assert [terminal_wire.decode_input(f).data for f in sock.sent_bytes] == [b"y"]
        assert session.store.input_mode == INPUT_MODE_TOOLS_KEYS
        assert session.view().keyboard == KEYBOARD_KEYS  # the latch drove
        # Leaving tools-keys fires the reset rule on the real store.
        assert session.set_keyboard(KEYBOARD_TEXT) is True
        assert session.store.input_mode == INPUT_MODE_COMPOSER
        # Multiline blocked send stops at explicit review.
        session.on_draft_change("do\nthis")
        assert await session.on_send() == SEND_BLOCKED_REVIEW
        pending = session.store.pending_blocked
        assert pending is not None
        assert (pending.review.line_count, pending.review.character_count) == (2, 7)
        assert session.composer_props()["review"] == pending.review
        frames_before = len(sock.sent_bytes)
        assert await session.on_confirm_blocked() is True
        sent = [terminal_wire.decode_input(f).data for f in sock.sent_bytes[frames_before:]]
        assert sent == [b"do\nthis"]
        assert session.view().keyboard == KEYBOARD_KEYS
        # Cancel path provably sends nothing.
        session.on_draft_change("no\npe")
        assert await session.on_send() == SEND_BLOCKED_REVIEW
        frames_before = len(sock.sent_bytes)
        assert session.on_cancel_blocked() is True
        assert session.store.pending_blocked is None
        assert len(sock.sent_bytes) == frames_before

    run(scenario())


def test_agent_disclosure_before_takeover_order_enforced() -> None:
    async def scenario() -> None:
        session, ops, factory, _ = _agent_session()
        # No disclosure, no attach: confirm raises before any gateway call.
        with pytest.raises(RuntimeError) as exc:
            await session.confirm_takeover()
        assert str(exc.value) == TAKEOVER_UNDISCLOSED_MESSAGE
        assert ops.attaches == []
        assert ops.leases == []
        # Open discloses WITHOUT attaching.
        assert session.open() == AGENT_CONNECTING
        assert session.takeover_disclosure == TAKEOVER_DISCLOSURE_AGENT
        assert ops.attaches == []
        # Cancelling disarms: confirm raises again, still no attach.
        session.cancel_takeover()
        assert session.takeover_disclosure is None
        with pytest.raises(RuntimeError) as exc:
            await session.confirm_takeover()
        assert str(exc.value) == TAKEOVER_UNDISCLOSED_MESSAGE
        assert ops.attaches == []
        # Explicit confirm after disclosure is the only attach path.
        assert session.request_takeover() == TAKEOVER_DISCLOSURE_AGENT
        assert await session.confirm_takeover() == AGENT_LIVE
        assert ops.attaches == [
            {"host_id": "home-mac", "terminal_id": "sess:1", "kind": "agent", "takeover": True}
        ]
        # Reconnect re-arms the disclosure and performs no attach itself.
        session.on_draft_change("kept")
        assert await session.disconnect() == AGENT_DISCONNECTED
        assert session.store.draft == "kept"
        attaches_before = list(ops.attaches)
        assert await session.reconnect() == AGENT_DISCONNECTED
        assert ops.attaches == attaches_before
        assert session.takeover_disclosure == TAKEOVER_DISCLOSURE_AGENT
        assert await session.confirm_takeover() == AGENT_LIVE
        assert len(ops.attaches) == len(attaches_before) + 1
        assert ops.attaches[-1]["takeover"] is True
        assert session.attachment is not None
        assert session.attachment.generation == 2
        assert session.store.draft == "kept"  # draft survives the round trip
        # Writer rebound: direct typing reaches the new generation.
        session.set_workflow(WORKFLOW_AGENT_DIRECT)
        assert await session.type_text("x") == "pty"
        chunks = [terminal_wire.decode_input(f) for f in factory.sockets[-1].sent_bytes]
        assert [(c.generation, c.input_seq) for c in chunks] == [(2, 1)]

    run(scenario())


def test_agent_mount_binds_real_store_through_rendered_screen() -> None:
    async def scenario() -> None:
        session, _, _, _ = _agent_session()
        # Disclosure renders before any attach.
        assert session.open() == AGENT_CONNECTING
        app = await render_component(mount_agent_screen, session)
        assert app.find(AGENT_TITLE_KEY).value == "agent-1"
        assert app.find(AGENT_TAKEOVER_DISCLOSURE_KEY) is not None
        assert app.find(DRAFT_KEY).value == ""
        assert app.find(SEND_KEY).disabled is True  # empty draft cannot send
        await app.set_props_and_fire(AGENT_TAKEOVER_CANCEL_KEY, {}, "click")
        assert session.takeover_disclosure is None
        # Live mount reflects the real store; fired events mutate it.
        session.request_takeover()
        assert await session.confirm_takeover() == AGENT_LIVE
        session.on_draft_change("hello agent")
        live = await render_component(mount_agent_screen, session)
        assert live.find(DRAFT_KEY).value == "hello agent"
        assert live.find(SEND_KEY).disabled is False
        await live.set_props_and_fire(DRAFT_KEY, {"value": "edited"}, "change", "edited")
        assert session.store.draft == "edited"
        # Blocked review renders from real pending state; cancel routes in.
        session.set_agent_status("blocked")
        session.on_draft_change("multi\nline")
        assert await session.on_send() == SEND_BLOCKED_REVIEW
        review_app = await render_component(mount_agent_screen, session)
        assert review_app.find("composer-blocked-review") is not None
        assert review_app.find("composer-blocked-preview").value == "multi\nline"
        assert review_app.find(SEND_KEY).disabled is True
        _absent(review_app, KEYS_DOCK_KEY)  # review pending: still text keyboard
        await review_app.set_props_and_fire("composer-blocked-cancel", {}, "click")
        assert session.store.pending_blocked is None
        # Blocked delivery latches the dock on the next render.
        session.on_draft_change("y")
        assert await session.on_send() == SEND_DELIVERED_VIA_ATTACH
        keys_app = await render_component(mount_agent_screen, session)
        assert keys_app.find(KEYS_DOCK_KEY) is not None
        assert keys_app.find(AGENT_KEYBOARD_TEXT_KEY).disabled is False

    run(scenario())


# -- retained-terminal registry integration (real sessions) --------------------


def test_registry_retains_shell_surfaces_and_evict_detaches() -> None:
    async def scenario() -> None:
        registry = TerminalRegistry()
        ops = ScriptedShellGatewayOps()
        factory = _SocketFactory()
        held: list[ShellSession] = []
        for index in range(5):
            terminal_id = f"term-{index}"
            shell = ShellSession(
                ops=ops,
                host_id="home-mac",
                device_id=ops.device_id,
                socket_factory=factory,
                clipboard=FakeClipboard(),
            )
            assert await shell.open(terminal_id) == SHELL_LIVE
            kept = await registry.retain("home-mac", terminal_id, shell, index * 1000)
            assert kept is shell
            held.append(shell)
        # Back navigation detaches the VIEW: still live, still buffering.
        registry.release("home-mac", "term-0", 9000)
        assert held[0].phase == SHELL_LIVE
        assert registry.is_idle("home-mac", "term-0") is True
        victim_lease = held[0].lease
        assert victim_lease is not None
        assert ops.is_live(victim_lease.lease_id) is True
        # A sixth surface evicts the idle LRU entry (term-0).
        sixth = ShellSession(
            ops=ops,
            host_id="home-mac",
            device_id=ops.device_id,
            socket_factory=factory,
            clipboard=FakeClipboard(),
        )
        assert await sixth.open("term-5") == SHELL_LIVE
        await registry.retain("home-mac", "term-5", sixth, 9100)
        # Eviction DETACHED term-0: lease released, remote pane survives.
        assert held[0].phase == SHELL_DISCONNECTED
        assert held[0].lease is None
        assert ops.is_live(victim_lease.lease_id) is False
        assert ops.releases == [victim_lease.lease_id]
        assert ops.is_closed("att-1") is False
        assert registry.keys("home-mac") == ("term-1", "term-2", "term-3", "term-4", "term-5")
        assert registry.get("home-mac", "term-0") is None

    run(scenario())


def test_registry_eviction_detaches_agent_and_preserves_draft() -> None:
    async def scenario() -> None:
        registry = TerminalRegistry()
        ops = ScriptedShellGatewayOps()
        factory = _SocketFactory()
        agent, _, _, _ = _agent_session(ops=ops, factory=factory)
        agent.open()
        assert await agent.confirm_takeover() == AGENT_LIVE
        agent.on_draft_change("unsent agent draft")
        assert await registry.retain("home-mac", "agent-1", agent, 0) is agent
        for index in range(4):
            shell = ShellSession(
                ops=ops,
                host_id="home-mac",
                device_id=ops.device_id,
                socket_factory=factory,
                clipboard=FakeClipboard(),
            )
            assert await shell.open(f"term-{index}") == SHELL_LIVE
            await registry.retain("home-mac", f"term-{index}", shell, 1000 + index)
        registry.release("home-mac", "agent-1", 9000)
        assert agent.phase == AGENT_LIVE  # Back keeps the stream live
        sixth = ShellSession(
            ops=ops,
            host_id="home-mac",
            device_id=ops.device_id,
            socket_factory=factory,
            clipboard=FakeClipboard(),
        )
        assert await sixth.open("term-9") == SHELL_LIVE
        await registry.retain("home-mac", "term-9", sixth, 9100)
        assert agent.phase == AGENT_DISCONNECTED
        assert agent.lease is None
        assert agent.store.draft == "unsent agent draft"  # eviction never eats the draft
        assert ops.is_closed("att-1") is False  # agent pane survives
        # Failed admission (all visible) leaves every surface alone.
        registry.set_visible("home-mac", registry.keys("home-mac"))
        extra = ShellSession(
            ops=ops,
            host_id="home-mac",
            device_id=ops.device_id,
            socket_factory=factory,
            clipboard=FakeClipboard(),
        )
        assert await extra.open("term-x") == SHELL_LIVE
        with pytest.raises(RetentionFullError):
            await registry.retain("home-mac", "term-x", extra, 9200)
        assert extra.phase == SHELL_LIVE
        assert sixth.phase == SHELL_LIVE

    run(scenario())


# -- fix round: agent conflict + submission interplay + drive refusal --


def test_agent_conflict_discloses_and_reconfirm_needs_no_redisclosure() -> None:
    """Foreign-held control lease: `confirm_takeover` lands in `conflict`
    with the disclosed owner AND the disclosure still armed, so once the
    holder releases, a bare re-confirm attaches (no re-disclosure)."""

    async def scenario() -> None:
        ops = _ControlHeldOps()
        ops.hold_control = True
        session, _, factory, _ = _agent_session(ops=ops)
        assert session.open() == AGENT_CONNECTING
        assert await session.confirm_takeover() == AGENT_CONFLICT
        assert session.owner_label == "dev-9"
        assert session.conflict_owner == "dev-9"
        assert session.controller is None
        assert session.attachment is None
        assert session.lease is None
        assert factory.sockets == []
        # The failed lease mints nothing AND leaves the disclosure armed.
        assert ops.tickets == []
        assert session.takeover_disclosure == TAKEOVER_DISCLOSURE_AGENT
        # Holder releases: a bare re-confirm attaches (no request_takeover).
        ops.hold_control = False
        ops._control_holder.clear()
        assert await session.confirm_takeover() == AGENT_LIVE
        assert session.takeover_disclosure is None
        assert session.conflict_owner is None
        assert session.controller is not None
        assert session.attachment is not None
        assert session.attachment.generation == 2
        assert session.keys_enabled is True

    run(scenario())


def test_agent_inflight_send_settles_unknown_on_takeover() -> None:
    """In-flight prompt send x takeover: the controller replacement settles
    the send as `unknown` (no replay), the draft is preserved, the late
    prompt outcome is discarded, and exactly one command was submitted."""

    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def gated_prompt(target: str, text: str) -> dict[str, Any]:
            entered.set()
            await release.wait()
            return {"ok": True, "target": target, "text": text}

        session, _, _, submitted = _agent_session(prompt=gated_prompt)
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        session.on_draft_change("hello agent")
        send_task = asyncio.ensure_future(session.on_send())
        await entered.wait()
        assert session.store.submission == SUBMISSION_SENDING
        assert len(submitted) == 1
        # Take over while the prompt is in flight: settle, then rebind.
        session.request_takeover()
        assert await session.confirm_takeover() == AGENT_LIVE
        assert session.attachment is not None
        assert session.attachment.generation == 2
        assert session.store.submission == SUBMISSION_UNKNOWN
        assert session.store.error == LOST_DELIVERY_MESSAGE
        assert session.store.draft == "hello agent"
        assert len(submitted) == 1
        # The late prompt outcome is discarded (send generation moved on).
        release.set()
        assert await send_task == SEND_IGNORED
        assert session.store.submission == SUBMISSION_UNKNOWN
        assert session.store.draft == "hello agent"
        assert len(submitted) == 1
        # No loss: the preserved draft re-sends explicitly (a new command).
        assert await session.on_send() == SEND_DELIVERED_VIA_PROMPT
        assert len(submitted) == 2
        assert submitted[0]["command_id"] != submitted[1]["command_id"]
        assert submitted[1]["payload"] == {"text": "hello agent"}
        assert session.store.draft == ""

    run(scenario())


def test_agent_blocked_review_dropped_on_reconnect_then_rebound() -> None:
    """Pending blocked review x reconnect: `reconnect()` drops the review
    (stale writer, nothing sent) and unbinds the writer; the confirming
    takeover rebinds it, so the same bytes then deliver exactly once."""

    async def scenario() -> None:
        session, _, factory, submitted = _agent_session(initial_status="blocked")
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        # Multiline blocked send parks at explicit review (still sending).
        session.on_draft_change("do\nthis")
        assert await session.on_send() == SEND_BLOCKED_REVIEW
        assert session.store.submission == SUBMISSION_SENDING
        assert session.store.pending_blocked is not None
        # Reconnect settles WITHOUT replay: review dropped, draft kept.
        assert await session.reconnect() == AGENT_DISCONNECTED
        assert session.store.pending_blocked is None
        assert session.store.submission == SUBMISSION_FAILED
        assert session.store.error == REVIEW_LOST_MESSAGE
        assert session.store.draft == "do\nthis"
        assert submitted == []
        assert session.takeover_disclosure == TAKEOVER_DISCLOSURE_AGENT
        # Unbound writer: a fresh blocked send fails closed (nothing sent).
        session.on_draft_change("y")
        (sock,) = factory.sockets
        frames_before = len(sock.sent_bytes)
        assert await session.on_send() == SEND_FAILED
        assert session.store.error == MISSING_WRITER_MESSAGE
        assert session.store.draft == "y"
        assert len(sock.sent_bytes) == frames_before
        # Confirm rebinds the live writer: the same bytes now deliver raw.
        assert await session.confirm_takeover() == AGENT_LIVE
        assert session.attachment is not None
        assert session.attachment.generation == 2
        assert await session.on_send() == SEND_DELIVERED_VIA_ATTACH
        assert submitted == []  # blocked path skips the RPC throughout
        sent = [terminal_wire.decode_input(f).data for f in factory.sockets[-1].sent_bytes]
        assert sent == [b"y"]
        assert session.store.draft == ""

    run(scenario())


def test_shell_drive_recovery_refuses_without_controller() -> None:
    """No controller (never opened): refuse without minting a ticket."""

    async def scenario() -> None:
        session, ops, _ = _session()
        assert session.controller is None
        assert await session.drive_recovery() == DRIVE_REFUSED_EXITED
        assert ops.tickets == []

    run(scenario())


def test_agent_drive_recovery_refuses_without_controller() -> None:
    """No controller (disclosure armed but unconfirmed): refuse without
    minting a ticket, mirroring the shell surface."""

    async def scenario() -> None:
        session, ops, _, _ = _agent_session()
        session.open()
        assert session.controller is None
        assert await session.drive_recovery() == DRIVE_REFUSED_EXITED
        assert ops.tickets == []

    run(scenario())
