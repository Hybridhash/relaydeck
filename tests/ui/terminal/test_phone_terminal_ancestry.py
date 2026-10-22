"""Phone chrome keeps the same native engine ancestry and guarded input."""

from __future__ import annotations

import asyncio
from typing import Any

import flet as ft
import pytest
from flet.data_channel import _ProtocolMuxedDataChannel
from relaydeck_terminal import protocol

from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.shell.screen import mount_shell_screen
from relaydeck.client.features.terminal.agent_session import AGENT_LIVE
from relaydeck.client.features.terminal.input_modes import (
    WORKFLOW_AGENT_COMPOSER,
    WORKFLOW_AGENT_DIRECT,
)
from relaydeck.client.features.terminal.surfaces import (
    TAKEOVER_MESSAGE,
    agent_hooks,
    mount_agent_screen,
)
from relaydeck.contracts.wire import terminal as terminal_wire
from tests.support.host_harness import button_label, render_component, run
from tests.support.terminal.direct_typing import open_compose
from tests.support.terminal.sessions import _agent_session, _session
from tests.support.ui.phone_terminal import _ancestry, _shape
from tests.support.ui.retained_root import _render
from tests.support.ui.window_design import _missing, _wrap_violations


def test_phone_focus_mode_and_keyboard_keep_confirmed_native_channel_and_output() -> None:
    async def scenario() -> None:
        session, ops, factory, submitted = _agent_session()
        session.open()
        assert await session.confirm_takeover() == AGENT_LIVE
        session.on_draft_change("first line\nمرحبا 🐾")
        app = await render_component(
            mount_agent_screen,
            session,
            phone=True,
            viewport_height=390,
            host_line="home-mac · workspace",
        )
        try:
            term, controller = session.current_term, session.controller
            assert term is not None and controller is not None
            channels: dict[int, Any] = {}

            def channel(number: int) -> Any:
                if number not in channels:
                    channels[number] = _ProtocolMuxedDataChannel(number, app.conn)
                return channels[number]

            app.conn.data_channel_for = channel
            await app.set_props_and_fire(
                "terminal-surface",
                {},
                "data_channel_open",
                {"channel_name": "terminal", "channel_id": 41},
            )
            binding = session._viewer_binding
            assert binding == (term, 1)
            channels[41]._deliver(protocol.encode_size_report(33, 19))
            await asyncio.sleep(agent_hooks.OPEN_HOLD_S + agent_hooks.RESIZE_SETTLE_S + 0.05)
            await app.settle()
            resize = terminal_wire.decode_input(factory.sockets[0].sent_bytes[-1])
            assert (resize.kind, resize.cols, resize.rows) == ("resize", 33, 19)
            assert resize.generation == controller.generation
            session._handle_bytes(b"synthetic greeting\r\n")
            output = bytes(session.output_log)
            draft = session.store.draft
            engine_shape = _shape(_ancestry(app, term))
            frames = list(app.conn.data_frames)
            assert app.find("terminal-flex-host").expand is True
            assert app.find("agent-input-dock").expand is None
            await open_compose(app)
            assert app.find("composer-draft").max_lines == 2
            assert app.find("composer-draft").multiline is True
            assert app.find("composer-draft").on_submit is None
            assert app.find("terminal-key-row").scroll is ft.ScrollMode.HIDDEN
            assert app.find("agent-workflow-toggle").width == 32
            assert app.find("agent-workflow-toggle").height == 32
            header = app.find("terminal-window-header")
            assert all(not isinstance(c, ft.Row) or not c.wrap for c in header.controls)
            assert app.find("agent-detail-title-host") not in list(iter_controls([header]))
            assert _wrap_violations(app) == []
            for c in iter_controls([app.page.views[0].controls]):
                if isinstance(c, ft.Row) and c.wrap:
                    assert all(not getattr(child, "expand", None) for child in c.controls)

            await app.set_props_and_fire("composer-draft", {}, "focus")
            channels[41]._deliver(protocol.encode_size_report(33, 3))
            await app.settle()
            assert _missing(app, "terminal-shortcuts")
            assert app.find("mobile-keybar").visible is False
            assert _shape(_ancestry(app, term)) == engine_shape
            await app.set_props_and_fire("composer-draft", {}, "blur")
            await app.set_props_and_fire("agent-workflow-toggle", {}, "click")
            assert session.view().workflow == WORKFLOW_AGENT_DIRECT
            assert app.find("mobile-keybar").visible is True
            assert _missing(app, "agent-keys-scroll")
            assert session.store.draft == draft
            await open_compose(app)
            assert session.view().workflow == WORKFLOW_AGENT_COMPOSER
            assert app.find("composer-draft").value == draft
            assert _shape(_ancestry(app, term)) == engine_shape
            assert session.current_term is term and session.controller is controller
            assert session._viewer_binding == binding and term.viewer_generation == 1
            assert controller.paste_generation() is not None
            assert bytes(session.output_log) == output
            # Neither input nor terminal display is reset/replayed by chrome changes.
            assert submitted == [] and len(ops.attaches) == 1
            assert app.conn.data_frames[: len(frames)] == frames
            assert all(
                payload[0] not in (protocol.RESET, protocol.WRITE)
                for _, payload in app.conn.data_frames[len(frames) :]
            )
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("width", [360, 390])
def test_phone_watching_takeover_exposes_consequences_and_details(width: int) -> None:
    async def scenario() -> None:
        bridge = RouteBridge(route="/", width=width, height=844)
        app, _controller, sessions, pool, _now = await _render(bridge=bridge)
        inventory_wraps = _wrap_violations(app)
        try:
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            session = sessions.factory.created[0]
            engine = session.current_term
            assert engine is not None and pool.foreground.session is session
            app.conn.data_channel_for = lambda number: _ProtocolMuxedDataChannel(number, app.conn)
            await app.set_props_and_fire(
                "terminal-surface",
                {},
                "data_channel_open",
                {"channel_name": "terminal", "channel_id": 42},
            )
            session.on_draft_change("kept watching draft 🐾")
            await app.settle()
            engine_shape = _shape(_ancestry(app, engine))
            binding = session._viewer_binding
            assert binding == (engine, 1)
            frames = list(app.conn.data_frames)
            panel = app.find("terminal-takeover-panel")
            assert isinstance(panel, ft.Column)
            assert panel.horizontal_alignment is ft.CrossAxisAlignment.STRETCH
            message = app.find("terminal-takeover-message")
            assert message.value == (
                "May resize terminal; disconnects other clients. "
                "While you control this pane it uses this device's size; it returns when you leave."
            )
            # Readable wrapping content, rather than hidden tooltip/semantics.
            assert message.max_lines is None and message.no_wrap is False
            assert message.overflow is not ft.TextOverflow.ELLIPSIS
            assert _missing(app, "terminal-takeover-details")
            assert button_label(app.find("terminal-takeover-details-toggle")) == "Details"
            assert app.find("terminal-takeover-actions").scroll is ft.ScrollMode.AUTO
            assert app.find("terminal-takeover-details-toggle").height == 48
            assert app.find("agent-takeover-confirm").height == 48
            assert sessions.ops.attaches == []
            for _ in range(2):
                await app.set_props_and_fire("terminal-takeover-details-toggle", {}, "click")
                full = app.find("terminal-takeover-details")
                assert full.value == TAKEOVER_MESSAGE
                assert full.max_lines is None and full.no_wrap is False
                assert full.overflow is not ft.TextOverflow.ELLIPSIS
                assert button_label(app.find("terminal-takeover-details-toggle")) == "Hide details"
                assert _shape(_ancestry(app, engine)) == engine_shape
                assert session.current_term is engine and session._viewer_binding == binding
                assert engine.viewer_generation == 1
                assert session.store.draft == "kept watching draft 🐾"
                assert sessions.ops.attaches == []
                await app.set_props_and_fire("terminal-takeover-details-toggle", {}, "click")
                assert _missing(app, "terminal-takeover-details")
                assert _shape(_ancestry(app, engine)) == engine_shape
            await app.set_props_and_fire("terminal-takeover-details-toggle", {}, "click")
            await app.set_props_and_fire("agent-takeover-cancel", {}, "click")
            assert sessions.ops.attaches == []
            assert _shape(_ancestry(app, engine)) == engine_shape
            assert all(
                payload[0] not in (protocol.RESET, protocol.WRITE)
                for _, payload in app.conn.data_frames[len(frames) :]
            )
            await app.set_props_and_fire("agent-takeover", {}, "click")
            assert _missing(app, "terminal-takeover-details")
            await app.set_props_and_fire("terminal-takeover-details-toggle", {}, "click")
            assert app.find("terminal-takeover-details").value == TAKEOVER_MESSAGE
            assert sessions.ops.attaches == []
            await app.set_props_and_fire("agent-takeover-confirm", {}, "click")
            assert len(sessions.ops.attaches) == 1 and session.phase == AGENT_LIVE
            assert session.current_term is engine
            assert session.store.draft == "kept watching draft 🐾"
            # Scope this check to changes introduced by the terminal; the
            # inventory's existing wrapping action rows are out of scope.
            assert _wrap_violations(app) == inventory_wraps
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


def test_phone_shell_has_one_header_and_keys_without_agent_composer() -> None:
    async def scenario() -> None:
        session, _ops, _factory = _session()
        await session.open("term-1")
        app = await render_component(
            mount_shell_screen,
            session,
            on_back=lambda: None,
            on_new_terminal=lambda: None,
            phone=True,
        )
        assert app.find("shell-terminal-host").expand is True
        assert app.find("terminal-key-row").scroll is ft.ScrollMode.HIDDEN
        for key in ("shell-input-axes", "composer-draft", "shell-direct-note"):
            assert _missing(app, key)
        assert _wrap_violations(app) == []
        await session.dispose()

    run(scenario())


def test_compact_composer_preserves_blocked_review_and_explicit_send_gate() -> None:
    async def scenario() -> None:
        session, _ops, _factory, submitted = _agent_session()
        session.open()
        await session.confirm_takeover()
        session.set_agent_status("blocked")
        session.on_draft_change("line one\nمرحبا 🐾")
        app = await render_component(mount_agent_screen, session, phone=True)
        await open_compose(app)
        try:
            draft = session.store.draft
            session.sessions.ime.begin()
            await app.session.dispatch_event(app.find("composer-draft").suffix_icon._i, "click", "")
            await app.settle()
            assert session.store.pending_blocked is None and session.store.draft == draft
            assert _missing(app, "composer-blocked-review") and submitted == []
            session.sessions.ime.cancel()
            await app.session.dispatch_event(app.find("composer-draft").suffix_icon._i, "click", "")
            await app.settle()
            assert app.find("composer-blocked-preview").value == draft
            assert app.find("composer-draft").suffix_icon.disabled
            assert session.store.pending_blocked is not None and submitted == []
            await app.set_props_and_fire("composer-blocked-cancel", {}, "click")
            assert session.store.pending_blocked is None and session.store.draft == draft
            assert submitted == []
        finally:
            await session.dispose()

    run(scenario())
