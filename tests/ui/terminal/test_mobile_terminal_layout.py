"""Host-dispatched layout/input checks; Flutter pixels/device IME stay pending."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import flet as ft
import pytest
from flet.data_channel import _ProtocolMuxedDataChannel
from relaydeck_terminal import protocol

from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.shell.screen import mount_shell_screen
from relaydeck.client.features.terminal.input_modes import (
    WORKFLOW_AGENT_COMPOSER,
    WORKFLOW_AGENT_DIRECT,
)
from relaydeck.client.features.terminal.key_bar import KEY_BAR
from relaydeck.client.features.terminal.surfaces import (
    mount_agent_screen,
    request_terminal_keyboard,
)
from relaydeck.domain import reducers
from relaydeck.domain.models import ConnectionState
from relaydeck.gateway import terminal_wire
from tests.support.host_harness import render_component, run
from tests.support.terminal.direct_typing import open_compose, open_direct_input
from tests.support.terminal.sessions import _agent_session, _session
from tests.support.ui.phone_terminal import _ancestry, _shape
from tests.support.ui.retained_root import _active, _render
from tests.support.ui.window_design import _missing, _wrap_violations


def _idempotent_channels(conn: Any) -> Any:
    """A data channel lookup that returns one object per id, as Flet's connection does; a
    terminal treats a different object for its id as a stale channel and holds its sends."""
    channels: dict[int, Any] = {}

    def lookup(number: int) -> Any:
        return channels.setdefault(number, _ProtocolMuxedDataChannel(number, conn))

    return lookup


@pytest.mark.parametrize("width,height", [(360, 800), (390, 844), (768, 1024), (1280, 800)])
def test_mobile_states_retain_root_engine_draft_and_width_specific_chrome(
    width: int, height: int
) -> None:
    async def scenario() -> None:
        bridge = RouteBridge(width=width, height=height)
        app, controller, sessions, pool, _now = await _render(bridge=bridge)
        try:
            baseline_wraps = _wrap_violations(app)
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            session = pool.foreground.session
            await app.set_props_and_fire("agent-takeover-confirm", {}, "click")
            term = session.current_term
            app.conn.data_channel_for = _idempotent_channels(app.conn)
            await app.set_props_and_fire(
                "terminal-surface",
                {},
                "data_channel_open",
                {"channel_name": "terminal", "channel_id": 71},
            )
            draft = "kept\nمرحبا 🐾"
            session.on_draft_change(draft)
            session._handle_bytes(b"synthetic greeting\r\n")
            await app.settle()
            ancestry = _shape(_ancestry(app, term))
            binding = session._viewer_binding
            frames = list(app.conn.data_frames)
            output = bytes(session.output_log)
            if width >= 1024:
                assert app.find("terminal-window-header").height is None
                assert app.find("composer-draft").value == draft
                assert app.find("terminal-area").border is not None
                assert _missing(app, "mobile-keybar")
                await app.set_props_and_fire("agent-workflow-direct", {}, "click")
                await app.set_props_and_fire("agent-workflow-composer", {}, "click")
            else:
                # A: full-width viewport, compact top line + reading controls.
                assert app.find("terminal-window-header").height == 28
                assert app.find("terminal-area").border is None
                assert app.find("terminal-wrap").padding.left == 0
                if width < 600:
                    assert app.find("inventory-content").padding == 0
                    assert _missing(app, "connection-banner")
                # Excluded from the tree, not merely invisible.
                with pytest.raises(KeyError):
                    app.find("terminal-toolbar")
                assert app.find("agent-tab-row").height == 40
                assert app.find("agent-tab-row").visible is False  # shown in Direct input only
                # In control, the idle text box shows and the keys live in the tools popover.
                assert app.find("composer-box").visible is True
                assert app.find("composer-draft").can_request_focus is True
                assert app.find("mobile-keybar").visible is False
                assert app.find("terminal-key-row").wrap == (width >= 600)
                assert app.find("mobile-keybar").height == (80 if width >= 600 else 40)
                assert _missing(app, "agent-keyboard-keys") and _missing(app, "agent-keys-scroll")
                for item in KEY_BAR:
                    if item.kind == "key" or item.kind == "modifier":
                        button = app.find(item.id)
                        assert button.height == 40 and button.width >= 40
                        assert button.content.height == 34
                        assert button.content.content.size == (
                            16 if isinstance(button.content.content, ft.Icon) else 13
                        )
                assert app.find("agent-switcher-open")  # switching lives in the header title
                # B: production resize carrier updates the RouteBridge, not a fake typed event.
                await open_direct_input(app)
                assert session.view().workflow == WORKFLOW_AGENT_DIRECT
                app.page.on_resize = lambda e: bridge.set_size(app.page.width, app.page.height)
                app.session.apply_patch(app.page._i, {"width": width, "height": height - 300})
                await app.session.dispatch_event(
                    app.page._i, "resize", {"width": width, "height": height - 300}
                )
                await app.settle()
                # The keyboard toggle stays reachable for one-tap dismissal.
                assert app.find("agent-tab-row").visible is True
                assert app.find("mobile-keybar").visible is True
                assert _shape(_ancestry(app, term)) == ancestry
                app.session.apply_patch(app.page._i, {"height": height})
                await app.session.dispatch_event(
                    app.page._i, "resize", {"width": width, "height": height}
                )
                await app.settle()
                assert app.find("agent-tab-row").visible is True
                # C: explicit compose; suffix send is inside the growable field.
                await open_compose(app)
                assert session.view().workflow == WORKFLOW_AGENT_COMPOSER
                field = app.find("composer-draft")
                assert field.value == draft and field.max_lines == 2 and field.min_lines == 2
                assert field.size_constraints.min_height == 36 and field.text_size == 15
                assert field.suffix_icon.key == "composer-send" and field.suffix_icon.height == 28
                assert field.on_submit is None
                assert app.find("mobile-keybar").visible is False
                assert _missing(app, "agent-keys-scroll")
                assert not app.find("agent-tab-row").visible  # Direct input only
                assert app.find("agent-workflow-toggle").key == "agent-workflow-toggle"
                assert _shape(_ancestry(app, term)) == ancestry
                await open_direct_input(app)
                assert session.store.draft == draft
                assert app.find("mobile-keybar").visible is True
                assert _shape(_ancestry(app, term)) == ancestry
            assert session.current_term is term and session._viewer_binding == binding
            assert term.viewer_generation == 1 and bytes(session.output_log) == output
            assert session.store.draft == draft and len(sessions.ops.attaches) == 1
            assert _wrap_violations(app) == baseline_wraps
            assert all(
                payload[0] not in (protocol.RESET, protocol.WRITE)
                for _, payload in app.conn.data_frames[len(frames) :]
            )
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


@pytest.mark.parametrize("width", [360, 390, 768, 1280])
def test_compact_root_removes_banner_sibling_but_dot_discloses_gap(width: int) -> None:
    async def scenario() -> None:
        app, controller, _sessions, pool, _now = await _render(bridge=RouteBridge(width=width))
        try:
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            session = pool.foreground.session
            term = session.current_term
            ancestry = _shape(_ancestry(app, term))
            state = reducers.set_connection(
                controller.state, ConnectionState.DISCONNECTED, "gap_stalled"
            )
            controller.push(replace(state, stale=True, needs_resync=True))
            await app.settle()
            if width < 600:
                assert app.find("inventory-content").padding == 0
                assert _missing(app, "connection-banner") and _missing(app, "banner-action")
            else:
                assert app.find("inventory-content").padding > 0
                assert "stopped after repeated gaps" in app.find("banner-text").value
            if width < 1024:
                dot = app.find("terminal-connection-dot")
                assert "stopped after repeated gaps" in dot.tooltip
                assert dot.items[0].content.value == dot.tooltip
                assert dot.items[1].key == "terminal-connection-retry"
            assert session.current_term is term and _shape(_ancestry(app, term)) == ancestry
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


def test_key_chip_text_uses_bundled_glyphs_and_preserves_semantics() -> None:
    async def scenario() -> None:
        session, _ops, _factory, _submitted = _agent_session()
        session.open()
        await session.confirm_takeover()
        app = await render_component(mount_agent_screen, session, phone=True)
        try:
            # fonttools cmap probe: absent from BOTH bundled text fonts.
            missing = frozenset("⇧⌫⏎←→")
            icons = {
                "key-left": ft.Icons.ARROW_BACK,
                "key-right": ft.Icons.ARROW_FORWARD,
                "key-up": ft.Icons.ARROW_UPWARD,
                "key-down": ft.Icons.ARROW_DOWNWARD,
                "key-backspace": ft.Icons.BACKSPACE_OUTLINED,
                "key-enter": ft.Icons.KEYBOARD_RETURN,
            }
            controls = list(iter_controls([app.find("mobile-keybar")]))
            assert all(not (set(c.value) & missing) for c in controls if isinstance(c, ft.Text))
            for item in KEY_BAR:
                if item.kind == "action":
                    continue
                button = app.find(item.id)
                semantics = next(
                    c for c in controls if isinstance(c, ft.Semantics) and c.content is button
                )
                assert semantics.label == item.semantics
                assert button.tooltip is None
                content = button.content.content
                if item.id in icons:
                    assert isinstance(content, ft.Icon) and content.icon == icons[item.id]
                elif item.id == "key-shift-tab":
                    assert isinstance(content, ft.Text) and content.value == "S-Tab"
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("width", [360, 390])
def test_explicit_keyboard_request_enables_input_first_and_fences_writer(
    shell: bool, width: int
) -> None:
    async def scenario() -> None:
        if shell:
            session, _ops, factory = _session()
            await session.open("term-1")
            app = await render_component(
                mount_shell_screen,
                session,
                on_back=lambda: None,
                on_new_terminal=lambda: None,
                phone=True,
                viewport_height=width,
            )
            key = "shell-keyboard-toggle"
        else:
            session, _ops, factory, _submitted = _agent_session()
            session.open()
            await session.confirm_takeover()
            app = await render_component(
                mount_agent_screen, session, phone=True, viewport_height=width
            )
            key = "agent-workflow-toggle"
        try:
            term = session.current_term
            app.conn.data_channel_for = _idempotent_channels(app.conn)
            await app.set_props_and_fire(
                "terminal-surface",
                {},
                "data_channel_open",
                {"channel_name": "terminal", "channel_id": 75},
            )
            binding = session._viewer_binding
            ancestry = _shape(_ancestry(app, term))
            sent = list(factory.sockets[0].sent_bytes)

            def requests() -> int:
                return sum(
                    payload == protocol.encode_request_keyboard()
                    for _, payload in app.conn.data_frames
                )

            assert requests() == 0  # neither mount nor channel open asks for an IME
            if not shell:
                await app.set_props_and_fire("agent-compose-toggle", {}, "click")
                request_terminal_keyboard(session, session.input_token, agent_direct=True)
                assert requests() == 0 and session.view().workflow == WORKFLOW_AGENT_COMPOSER
                key = "agent-workflow-toggle"
            before = len(app.conn.data_frames)
            await app.set_props_and_fire(key, {}, "click")
            packets = [p for _, p in app.conn.data_frames[before:]]
            at = packets.index(protocol.encode_request_keyboard())
            assert at > 0 and packets[at - 1] == protocol.encode_input_enabled(True)
            assert requests() == 1

            key = "shell-keyboard-toggle" if shell else "agent-workflow-toggle"
            action = app.find(key)
            token = session.input_token
            # Selection, loss of control lease, stale token and parked callbacks fail closed.
            session.open_selection()
            request_terminal_keyboard(session, token, agent_direct=not shell)
            assert requests() == 1
            session.close_selection()
            mode = session.controller.mode
            session.controller.mode = "view"
            request_terminal_keyboard(session, session.input_token, agent_direct=not shell)
            assert requests() == 1
            session.controller.mode = mode
            session.set_foreground(False)
            await app.session.dispatch_event(action._i, "click", "")
            session.set_foreground(True)
            await app.session.dispatch_event(action._i, "click", "")
            request_terminal_keyboard(session, token, agent_direct=not shell)
            await app.settle()
            assert requests() == 1
            assert session.current_term is term and session._viewer_binding == binding
            assert _shape(_ancestry(app, term)) == ancestry
            assert factory.sockets[0].sent_bytes == sent
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("shell", [False, True])
def test_mobile_latch_and_long_press_emit_guarded_exact_bytes(shell: bool) -> None:
    async def scenario() -> None:
        if shell:
            session, ops, factory = _session()
            await session.open("term-1")
            app = await render_component(
                mount_shell_screen,
                session,
                on_back=lambda: None,
                on_new_terminal=lambda: None,
                phone=True,
            )
        else:
            session, ops, factory, _submitted = _agent_session()
            session.open()
            await session.confirm_takeover()
            app = await render_component(mount_agent_screen, session, phone=True)
        try:
            socket = factory.sockets[0]

            def inputs() -> list[bytes]:
                return [
                    frame.data
                    for raw in socket.sent_bytes
                    if (frame := terminal_wire.decode_input(raw)).kind == "text"
                ]

            await app.set_props_and_fire("mod-ctrl", {}, "click")
            assert app.find("mod-ctrl").data["latch"] == "once"
            assert session.sessions.keys.is_modifier_armed("ctrl")
            await app.set_props_and_fire("key-ctrl-c", {}, "click")
            assert inputs()[-1] == b"\x03"
            assert app.find("mod-ctrl").data["latch"] == "off"
            # Actual duplicate dispatch (no fake latch calls, no timing sleeps).
            button = app.find("mod-ctrl")
            await app.session.dispatch_event(button._i, "click", "")
            await app.session.dispatch_event(button._i, "click", "")
            await app.settle()
            assert app.find("mod-ctrl").data["latch"] == "locked"
            for key, expected in [("key-ctrl-d", b"\x04"), ("key-ctrl-l", b"\x0c")]:
                await app.set_props_and_fire(key, {}, "click")
                assert inputs()[-1] == expected
                assert app.find("mod-ctrl").data["latch"] == "locked"
                assert session.sessions.keys.is_modifier_armed("ctrl")
            await app.set_props_and_fire("mod-ctrl", {}, "click")
            assert app.find("mod-ctrl").data["latch"] == "off"
            for key, expected in [
                ("key-up", b"\x1b[5~"),
                ("key-down", b"\x1b[6~"),
                ("key-left", b"\x1b[H"),
                ("key-right", b"\x1b[F"),
                ("key-backspace", b"\x17"),
            ]:
                await app.set_props_and_fire(key, {}, "long_press")
                assert inputs()[-1] == expected
            old = app.find("key-ctrl-c")
            session.set_foreground(False)
            before = inputs()
            await app.session.dispatch_event(old._i, "click", "")
            await app.settle()
            assert inputs() == before  # parked callback cannot write to the current generation
        finally:
            await session.dispose()

    run(scenario())


def test_keybar_restores_scroll_offset_after_composing() -> None:
    async def scenario() -> None:
        session, _ops, _factory, _submitted = _agent_session()
        session.open()
        await session.confirm_takeover()
        app = await render_component(mount_agent_screen, session, phone=True)
        try:
            await app.set_props_and_fire(
                "terminal-key-row",
                {},
                "scroll",
                {
                    "event_type": "update",
                    "pixels": 237.0,
                    "min_scroll_extent": 0.0,
                    "max_scroll_extent": 1000.0,
                    "viewport_dimension": 390.0,
                    "scroll_delta": 237.0,
                },
            )
            await app.set_props_and_fire("agent-compose-toggle", {}, "click")
            start = len(app.conn.sent)
            await app.set_props_and_fire("agent-workflow-toggle", {}, "click")
            requests = [
                message.body
                for message in app.conn.sent[start:]
                if getattr(message.body, "name", None) == "scroll_to"
            ]
            assert len(requests) == 1 and requests[0].args["offset"] == 237.0
            request = requests[0]
            # Acknowledge the actual Flet request; this is command delivery, not pixel proof.
            app.session.handle_invoke_method_results(
                request.control_id, request.call_id, None, None
            )
            await app.settle()
        finally:
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("width", [390, 768, 1280])
def test_operations_chrome_hides_only_in_compact_and_more_does_not_remount(width: int) -> None:
    from relaydeck.client.pending_commands import MemoryCommandStore, PendingCommands

    async def scenario() -> None:
        pending = PendingCommands(
            MemoryCommandStore(), "https://private.test", "synthetic", "device"
        )
        pending.error = "Synthetic pending storage needs attention."
        app, _controller, sessions, pool, _now = await _render(
            bridge=RouteBridge(width=width, height=844), pending_commands=pending
        )
        try:
            assert app.find("terminal-operations-bar").visible is True
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            session = pool.foreground.session
            term = session.current_term
            ancestry = _shape(_ancestry(app, term))
            session.on_draft_change("retained operations draft")
            await app.settle()
            assert app.find("terminal-operations-bar").visible == (width >= 600)
            if width < 600:
                await app.set_props_and_fire("terminal-operations", {}, "click")
                assert app.find("terminal-operations-sheet") is not None
                assert app.find("terminal-operations-bar").visible is False
                assert _shape(_ancestry(app, term)) == ancestry
                await app.set_props_and_fire("terminal-operations-close", {}, "click")
                assert _missing(app, "terminal-operations-sheet")
                assert _shape(_ancestry(app, term)) == ancestry
                await app.set_props_and_fire("terminal-back", {}, "click")
                assert app.find("terminal-operations-bar").visible is True
                assert app.find("phone-nav-slot").visible is True
            assert (
                session.current_term is term and session.store.draft == "retained operations draft"
            )
            assert sessions.ops.attaches == []
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


@pytest.mark.parametrize("width", [360, 390, 768, 1280])
def test_shell_width_states_preserve_channel_without_composer(width: int) -> None:
    from relaydeck.client.features.terminal.key_bar import width_class

    async def scenario() -> None:
        session, _ops, _factory = _session()
        await session.open("term-1")

        @ft.component
        def Parent() -> Any:
            opened, set_opened = ft.use_state(False)
            return ft.Column(
                controls=[
                    mount_shell_screen(
                        session,
                        key="test-shell",
                        on_back=lambda: None,
                        on_new_terminal=lambda: None,
                        width_tier=width_class(width),
                        keyboard_open=opened,
                    ),
                    ft.Button(
                        "Resize",
                        key="test-keyboard-resize",
                        on_click=lambda e: set_opened(not opened),
                    ),
                ]
            )

        app = await render_component(Parent)
        try:
            term = session.current_term
            app.conn.data_channel_for = _idempotent_channels(app.conn)
            await app.set_props_and_fire(
                "terminal-surface",
                {},
                "data_channel_open",
                {"channel_name": "terminal", "channel_id": 73},
            )
            ancestry = _shape(_ancestry(app, term))
            binding = session._viewer_binding
            for _ in range(2):
                await app.set_props_and_fire("test-keyboard-resize", {}, "click")
                assert _shape(_ancestry(app, term)) == ancestry
                assert session.current_term is term and session._viewer_binding == binding
                assert _missing(app, "composer-draft") and _missing(app, "agent-tab-row")
                if width < 1024:
                    assert app.find("terminal-window-header").height == 28
                    assert app.find("terminal-area").border is None
                    assert _missing(app, "shell-keys-scroll")
                    assert app.find("mobile-keybar").visible is True
                    assert app.find("key-esc").content.height == 34
                else:
                    assert _missing(app, "mobile-keybar")
                    assert app.find("terminal-window-header").height is None
            assert term.viewer_generation == 1
        finally:
            await session.dispose()

    run(scenario())


async def _switch_to(app: Any, key: str) -> None:
    """Header title, then the agent's row in the switcher list."""
    await app.set_props_and_fire("agent-switcher-open", {}, "click")
    row = _active(app, f"agent-switcher-row-{key}")
    await app.session.dispatch_event(row._i, "click", "")
    await app.settle()


def test_agent_tab_clicks_keep_retained_draft_and_never_take_over_another_agent() -> None:
    async def scenario() -> None:
        app, _controller, sessions, pool, _now = await _render(
            bridge=RouteBridge(width=390, height=844)
        )
        try:
            await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
            first = pool.foreground.session
            term = first.current_term
            first.on_draft_change("tab draft")
            await app.settle()
            before = len(sessions.ops.attaches)
            # Switch through the header list, without taking ownership or discarding the draft.
            await _switch_to(app, "home-mac:term-2")
            assert pool.foreground.session is not first
            assert first.current_term is term and first.store.draft == "tab draft"
            assert len(sessions.ops.attaches) == before
            await _switch_to(app, "home-mac:term-1")
            assert pool.foreground.session is first and first.current_term is term
            assert first.store.draft == "tab draft" and len(sessions.ops.attaches) == before
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())
