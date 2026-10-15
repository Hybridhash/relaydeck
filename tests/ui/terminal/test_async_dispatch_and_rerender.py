"""Async dispatch and visible rerender regressions.

Every test drives REAL Flet dispatched events through the host harness
 against the mounted agent screen — never direct domain awaits. Failing
 before the fix: sync lambdas wrap async session methods, so dispatch
 reports unawaited coroutines and nothing is sent; mode clicks mutate the
 model without rerendering.
"""

from __future__ import annotations

from typing import Any

from relaydeck.client.features.composer.components import DRAFT_KEY, SEND_KEY
from relaydeck.client.features.terminal.agent_session import (
    AGENT_LIVE,
    AgentSession,
)
from relaydeck.client.features.terminal.input_modes import (
    KEYBOARD_KEYS,
    WORKFLOW_AGENT_DIRECT,
)
from relaydeck.client.features.terminal.keys import KEYS_DOCK_KEY
from relaydeck.client.features.terminal.paste_review import CONFIRM_KEY, SHEET_KEY
from relaydeck.client.features.terminal.surfaces import (
    AGENT_TAKEOVER_CONFIRM_KEY,
    AGENT_WORKFLOW_COMPOSER_KEY,
    AGENT_WORKFLOW_DIRECT_KEY,
    mount_agent_screen,
)
from relaydeck.gateway import terminal_wire
from tests.support.host_harness import render_component
from tests.support.terminal.dispatch import _agent_session, _SocketFactory, run_clean


async def _live_session() -> tuple[AgentSession, _SocketFactory, list[dict[str, Any]]]:
    session, factory, submitted = _agent_session()
    assert session.open() is not None
    assert await session.confirm_takeover() == AGENT_LIVE
    return session, factory, submitted


def test_dispatched_draft_uses_payload_despite_stale_control() -> None:
    """Browser regression: native typing fired change with the new text
    in the payload while the indexed control still held the old value;
    the handler read the stale control, so text briefly appeared then
    reset on round-trip and Send never enabled. Fired here through the
    real session dispatch with NO property patch (stale control) and
    the payload set, exactly like the live client."""

    async def scenario() -> None:
        session, _, _ = await _live_session()
        app = await render_component(mount_agent_screen, session)
        assert app.find(DRAFT_KEY).value == ""
        assert app.find(SEND_KEY).disabled is True
        await app.set_props_and_fire(DRAFT_KEY, {}, "change", "typed live")
        assert session.store.draft == "typed live"
        assert app.find(DRAFT_KEY).value == "typed live"
        assert app.find(SEND_KEY).disabled is False

    run_clean(scenario())


def test_dispatched_send_delivers_exactly_once() -> None:
    async def scenario() -> None:
        session, _, submitted = await _live_session()
        session.on_draft_change("hello agent")
        app = await render_component(mount_agent_screen, session)
        assert app.find(SEND_KEY).disabled is False
        await app.set_props_and_fire(SEND_KEY, {}, "click")
        assert len(submitted) == 1
        assert submitted[0]["payload"] == {"text": "hello agent"}
        assert session.store.draft == ""

    run_clean(scenario())


def test_dispatched_tools_home_sends_exact_key_bytes() -> None:
    async def scenario() -> None:
        session, factory, _ = await _live_session()
        session.set_keyboard(KEYBOARD_KEYS)
        app = await render_component(mount_agent_screen, session)
        assert app.find(KEYS_DOCK_KEY) is not None
        # Enter lives in the inline row on desktop now;
        # the dock routes its own deduped keys with identical framing.
        await app.set_props_and_fire("shortcut-key-home", {}, "click")
        assert len(factory.sockets) == 1
        frames = factory.sockets[0].sent_bytes
        assert len(frames) == 1
        chunk = terminal_wire.decode_input(frames[0])
        assert chunk.data == b"\x1b[H"
        assert (chunk.generation, chunk.input_seq) == (1, 1)

    run_clean(scenario())


def test_direct_input_click_visibly_rerenders_and_preserves_draft() -> None:
    async def scenario() -> None:
        session, _, _ = await _live_session()
        session.on_draft_change("multi\nline\ndraft")
        app = await render_component(mount_agent_screen, session)
        assert app.find(DRAFT_KEY).value == "multi\nline\ndraft"
        assert app.find(AGENT_WORKFLOW_DIRECT_KEY).disabled is False
        await app.set_props_and_fire(AGENT_WORKFLOW_DIRECT_KEY, {}, "click")
        # The SAME mounted app must rerender: Direct disabled (active),
        # Composer enabled, draft field gone, model switched.
        assert session.sessions.modes.workflow == WORKFLOW_AGENT_DIRECT
        assert app.find(AGENT_WORKFLOW_DIRECT_KEY).disabled is True
        assert app.find(AGENT_WORKFLOW_COMPOSER_KEY).disabled is False
        try:
            app.find(DRAFT_KEY)
            raise AssertionError("composer draft must hide in Direct Input")
        except KeyError:
            pass
        # Back to Composer restores the hidden draft byte-identically.
        await app.set_props_and_fire(AGENT_WORKFLOW_COMPOSER_KEY, {}, "click")
        assert app.find(DRAFT_KEY).value == "multi\nline\ndraft"

    run_clean(scenario())


def test_dispatched_takeover_confirm_attaches() -> None:
    async def scenario() -> None:
        session, _, _ = _agent_session()
        session.open()
        app = await render_component(mount_agent_screen, session)
        await app.set_props_and_fire(AGENT_TAKEOVER_CONFIRM_KEY, {}, "click")
        assert session.phase == AGENT_LIVE
        assert session.attachment is not None

    run_clean(scenario())


def test_dispatched_paste_confirm_sends_reviewed_bytes() -> None:
    async def scenario() -> None:
        session, factory, _ = await _live_session()
        assert await session.request_paste("line1\nline2") == "review"
        app = await render_component(mount_agent_screen, session)
        assert app.find(SHEET_KEY) is not None
        await app.set_props_and_fire(CONFIRM_KEY, {}, "click")
        assert session.pending_review is None
        frames = factory.sockets[0].sent_bytes
        assert len(frames) == 1
        chunk = terminal_wire.decode_input(frames[0])
        assert chunk.data == b"line1\nline2"

    run_clean(scenario())


def test_dispatched_send_after_dispose_cannot_submit() -> None:
    async def scenario() -> None:
        session, _, submitted = await _live_session()
        session.on_draft_change("doomed")
        app = await render_component(mount_agent_screen, session)
        assert app.find(SEND_KEY).disabled is False
        saved_send = app.find(SEND_KEY).on_click
        await session.dispose()
        await app.settle()
        # The disposed window no longer offers Send; the handler that was
        # dispatched from the earlier render must still refuse.
        await saved_send(None)
        await app.settle()
        assert submitted == []
        assert session.store.draft == "doomed"

    run_clean(scenario())


def test_dispatched_blocked_confirm_inserts_without_enter() -> None:
    async def scenario() -> None:
        session, factory, submitted = _agent_session()
        assert session.open() is not None
        session.set_agent_status("blocked")
        assert await session.confirm_takeover() == AGENT_LIVE
        session.on_draft_change("multi\nline")
        assert await session.on_send() == "blocked-review"
        app = await render_component(mount_agent_screen, session)
        await app.set_props_and_fire("composer-blocked-confirm", {}, "click")
        # Blocked path: raw UTF-8 into the PTY, no Enter, no framing,
        # no agent.prompt command — delivered through real dispatch.
        assert submitted == []
        frames = factory.sockets[0].sent_bytes
        assert len(frames) == 1
        chunk = terminal_wire.decode_input(frames[0])
        assert chunk.data == b"multi\nline"
        assert session.store.submission == "blocked-inserted"

    run_clean(scenario())
